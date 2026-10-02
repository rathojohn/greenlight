"""OpenTelemetry in and out, over OTLP/HTTP with JSON bodies (standard library only).

Export turns what greenlight knows into traces and metrics any OTel backend can show:
  pipeline attempt  trace: "RUN <workflow>" (SERVER) > a queue span, one span per job, one per step
  test run          trace: the run, then one span per test execution (failures carry an exception event)
  deployment        span with deployment.* attributes and the commits it shipped
  metrics           cicd.pipeline.run.duration, cicd.pipeline.run.errors, vcs.change.count,
                    vcs.change.duration, vcs.change.time_to_merge, plus greenlight.* flake and DORA gauges
Names and attributes follow the OpenTelemetry semantic conventions for CI/CD, VCS, test and
deployment (all still in development upstream). Anything greenlight adds lives under greenlight.*.

Receive does the reverse for test and CI/CD spans, so an OTel-instrumented test runner, or a
Collector (including its GitHub receiver), can feed the gate without JUnit files.

Endpoints and headers come from the standard variables: OTEL_EXPORTER_OTLP_ENDPOINT (default
http://localhost:4318), OTEL_EXPORTER_OTLP_{TRACES,METRICS}_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS,
OTEL_EXPORTER_OTLP_{TRACES,METRICS}_HEADERS, OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable

from . import __version__, analysis
from .db import iso, parse_time, since, utcnow
from .ingest import TestResult, failure_signature, insert_results, record_run

SCOPE = {"name": "greenlight", "version": __version__}
INTERNAL, SERVER = 1, 2
UNSET, ERROR = 0, 2
BATCH = 1000
MAX_BODY = 32 * 1024 * 1024

# GitHub's conclusions, in the semantic conventions' words
RESULT = {"success": "success", "failure": "failure", "timed_out": "timeout", "cancelled": "cancellation",
          "skipped": "skip", "startup_failure": "error", "neutral": "success", "action_required": "error",
          "stale": "cancellation"}
FROM_RESULT = {"success": "success", "failure": "failure", "timeout": "timed_out", "cancellation": "cancelled",
               "skip": "skipped", "error": "failure"}
BAD = {"failure", "timeout", "error"}


# ---------- ids, time, attributes ----------
def trace_id(key: str) -> str:
    """Deterministic, so exporting the same run twice gives the same trace."""
    return hashlib.sha256(f"trace/{key}".encode()).hexdigest()[:32]


def span_id(key: str) -> str:
    return hashlib.sha256(f"span/{key}".encode()).hexdigest()[:16]


def nanos(value: str | datetime | None) -> int | None:
    dt = parse_time(value) if isinstance(value, str) or value is None else value
    return int(dt.timestamp() * 1_000_000_000) if dt else None


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_value(x) for x in v]}}
    return {"stringValue": str(v)}


def attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in d.items() if v is not None and v != ""]


def read_attrs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    out = {}
    for a in items or []:
        v = a.get("value") or {}
        if "stringValue" in v:
            out[a["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[a["key"]] = int(v["intValue"])
        elif "doubleValue" in v:
            out[a["key"]] = float(v["doubleValue"])
        elif "boolValue" in v:
            out[a["key"]] = bool(v["boolValue"])
        elif "arrayValue" in v:
            out[a["key"]] = [read_attrs([{"key": "x", "value": x}]).get("x") for x in v["arrayValue"].get("values", [])]
    return out


def span(tid: str, sid: str, name: str, start: int, end: int, attributes: dict[str, Any], parent: str | None = None,
         kind: int = INTERNAL, error: str | None = None, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    s = {"traceId": tid, "spanId": sid, "name": name, "kind": kind,
         "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)), "attributes": attrs(attributes),
         "status": {"code": ERROR, "message": error} if error else {"code": UNSET}}
    if parent:
        s["parentSpanId"] = parent
    if events:
        s["events"] = events
    return s


# ---------- resource and endpoints ----------
def resource(repo: str | None, service_name: str | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {}
    for pair in filter(None, (os.environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").split(",")):
        k, _, v = pair.partition("=")
        if k.strip():
            base[k.strip()] = urllib.parse.unquote(v.strip())
    name = os.environ.get("OTEL_SERVICE_NAME") or service_name or base.get("service.name") or (
        repo.split("/")[-1] if repo else "greenlight")
    base.update({"service.name": name, "telemetry.sdk.name": "greenlight", "telemetry.sdk.language": "python",
                 "telemetry.sdk.version": __version__})
    if repo:
        owner, _, rname = repo.partition("/")
        base.update({"vcs.provider.name": "github", "vcs.owner.name": owner, "vcs.repository.name": rname,
                     "vcs.repository.url.full": f"https://github.com/{repo}"})
    return {"attributes": attrs(base)}


def endpoint(signal: str, configured: str | None = None) -> str:
    specific = os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    base = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or configured or "http://localhost:4318").rstrip("/")
    return f"{base}/v1/{signal}"


def headers(signal: str) -> dict[str, str]:
    out = {}
    for var in ("OTEL_EXPORTER_OTLP_HEADERS", f"OTEL_EXPORTER_OTLP_{signal.upper()}_HEADERS"):
        for pair in filter(None, (os.environ.get(var) or "").split(",")):
            k, _, v = pair.partition("=")
            if k.strip():
                out[k.strip()] = urllib.parse.unquote(v.strip())
    return out


class ExportError(RuntimeError):
    pass


def post(url: str, payload: dict[str, Any], extra_headers: dict[str, str], timeout: float = 30) -> dict[str, Any]:
    body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    h = {"Content-Type": "application/json", "Content-Encoding": "gzip", "User-Agent": f"greenlight/{__version__}",
         **extra_headers}
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx if url.startswith("https") else None) as r:
                raw = r.read()
                return json.loads(raw) if raw and raw.strip().startswith(b"{") else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(1 + 2 * attempt)
                continue
            detail = e.read(300).decode(errors="replace") if e.fp else ""
            raise ExportError(f"{url} answered {e.code}: {detail or e.reason}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(1 + 2 * attempt)
                continue
            raise ExportError(f"Could not reach {url}: {getattr(e, 'reason', e)}. Is a Collector or backend listening?") from None
    raise ExportError(f"{url}: gave up after 3 attempts")


# ---------- traces: pipelines ----------
def _task_type(name: str) -> str | None:
    n = name.lower()
    if any(w in n for w in ("test", "e2e", "spec", "check")):
        return "test"
    if any(w in n for w in ("deploy", "release", "publish", "ship")):
        return "deploy"
    if any(w in n for w in ("build", "compile", "bundle", "package")):
        return "build"
    return None


def pipeline_spans(conn: sqlite3.Connection, p: sqlite3.Row) -> list[dict[str, Any]]:
    key = p["pipeline_id"]
    tid, root = trace_id(key), span_id(key)
    result = RESULT.get(p["status"], "error")
    start = nanos(p["started_at"] or p["created_at"])
    end = nanos(p["finished_at"]) or start
    run_id = key.split(":")[1] if key.count(":") >= 2 else key
    spans = [span(tid, root, f"RUN {p['workflow']}", start, end, {
        "cicd.pipeline.name": p["workflow"], "cicd.pipeline.run.id": run_id, "cicd.pipeline.action.name": "RUN",
        "cicd.pipeline.result": result, "cicd.pipeline.run.url.full": p["url"],
        "error.type": result if result in BAD else None,
        "vcs.ref.head.name": p["branch"], "vcs.ref.head.revision": p["commit_sha"],
        "greenlight.pipeline.attempt": p["attempt"], "greenlight.pipeline.event": p["event"],
        "greenlight.pipeline.actor": p["actor"], "greenlight.pipeline.queue_ms": p["queue_ms"],
    }, kind=SERVER, error=f"pipeline {result}" if result in BAD else None)]
    if p["queue_ms"] and p["attempt"] == 1 and p["created_at"]:
        q0 = nanos(p["created_at"])
        spans.append(span(tid, span_id(key + "/queue"), "queue", q0, q0 + p["queue_ms"] * 1_000_000,
                          {"cicd.pipeline.run.state": "pending"}, parent=root))
    for j in conn.execute("SELECT * FROM jobs WHERE pipeline_id = ? ORDER BY started_at", (key,)):
        jr = RESULT.get(j["status"], "error")
        js = nanos(j["started_at"]) or start
        je = nanos(j["finished_at"]) or (js + (j["duration_ms"] or 0) * 1_000_000)
        jid = span_id(j["job_id"])
        spans.append(span(tid, jid, j["name"], js, je, {
            "cicd.pipeline.task.name": j["name"], "cicd.pipeline.task.run.id": j["job_id"].split(":", 1)[-1],
            "cicd.pipeline.task.run.result": jr, "cicd.pipeline.task.run.url.full": j["url"],
            "cicd.pipeline.task.type": _task_type(j["name"]), "cicd.worker.name": j["runner"],
            "error.type": jr if jr in BAD else None, "greenlight.job.queue_ms": j["queue_ms"],
        }, parent=root, error=f"job {jr}" if jr in BAD else None))
        cursor = js
        for st in conn.execute("SELECT * FROM steps WHERE job_id = ? ORDER BY number", (j["job_id"],)):
            ss = nanos(st["started_at"]) or cursor
            se = ss + (st["duration_ms"] or 0) * 1_000_000
            cursor = se
            sr = RESULT.get(st["status"], "error")
            spans.append(span(tid, span_id(f"{j['job_id']}/{st['number']}"), st["name"], ss, se, {
                "greenlight.step.number": st["number"], "greenlight.step.result": sr,
            }, parent=jid, error=f"step {sr}" if sr in BAD else None))
    return spans


# ---------- traces: test runs ----------
def run_spans(conn: sqlite3.Connection, run: sqlite3.Row, classes: dict[str, str]) -> list[dict[str, Any]]:
    key = run["external_id"] or f"run:{run['run_id']}"
    tid, root = trace_id(key), span_id(key)
    rows = conn.execute("SELECT * FROM results WHERE run_id = ? AND COALESCE(flags, '') NOT LIKE '%inferred%' "
                        "ORDER BY retry, test_id", (run["run_id"],)).fetchall()
    t = analysis.triage_run(conn, run_id=run["run_id"])
    category = {f["test_id"]: f["category"] for f in t["failures"]}
    start = nanos(run["started_at"])
    cursor, children = start, []
    for r in rows:
        dur = (r["duration_ms"] or 0) * 1_000_000
        status = {"pass": "pass", "fail": "fail", "error": "fail"}.get(r["outcome"])
        failed = status == "fail"
        events = None
        if failed:
            events = [{"timeUnixNano": str(cursor + dur), "name": "exception",
                       "attributes": attrs({"exception.type": "TestFailure", "exception.message": r["message"]})}]
        suite, _, case = r["test_id"].rpartition("::")
        children.append(span(tid, span_id(f"{key}/{r['test_id']}/{r['retry']}"), r["test_id"], cursor, cursor + dur, {
            "test.case.name": r["test_id"], "test.suite.name": suite or None, "test.case.result.status": status,
            "code.function.name": case or None, "code.file.path": r["file"],
            "greenlight.test.outcome": r["outcome"], "greenlight.test.retry": r["retry"],
            "greenlight.test.flags": r["flags"], "greenlight.test.classification": classes.get(r["test_id"]),
            "greenlight.test.category": category.get(r["test_id"]) if failed else None,
            "greenlight.test.failure_signature": r["failure_sig"],
        }, parent=root, error=(r["message"] or "test failed")[:200] if failed else None, events=events))
        cursor += dur
    end = max(cursor, start + (run["duration_ms"] or 0) * 1_000_000)
    any_fail = bool(t["failures"])
    git_commit = run["git_commit"] or run["commit_sha"].split("+")[0].split("@")[0]
    root_span = span(tid, root, f"test {run['session'] or run['source'] or 'run'}", start, end, {
        "test.suite.name": run["session"] or run["source"], "test.suite.run.status": "failure" if any_fail else "success",
        "vcs.ref.head.name": run["branch"], "vcs.ref.head.revision": git_commit,
        "cicd.pipeline.run.url.full": run["url"], "greenlight.run.id": run["run_id"],
        "greenlight.run.attempt": run["attempt"], "greenlight.run.code_id": run["commit_sha"],
        "greenlight.run.source": run["source"], "greenlight.run.command": run["command"],
        "greenlight.gate.decision": t["decision"], "greenlight.gate.blocking": len(t["blocking"]),
        "greenlight.gate.rerun": len(t["rerun_tests"]), "greenlight.run.tests": t["run"]["tests"],
        "greenlight.timing": "test spans are laid end to end from the run start; JUnit carries durations, not start times",
    }, kind=SERVER, error=f"{t['decision']}: {t['summary']}" if t["decision"] == "REAL_FAILURE" else None)
    return [root_span, *children]


# ---------- traces: deployments ----------
def deployment_spans(conn: sqlite3.Connection, d: sqlite3.Row) -> list[dict[str, Any]]:
    key = d["deploy_id"]
    tid = trace_id(key)
    at = nanos(d["deployed_at"])
    commits = conn.execute("SELECT authored_at FROM deploy_commits WHERE deploy_id = ?", (key,)).fetchall()
    leads = sorted((parse_time(d["deployed_at"]) - parse_time(c["authored_at"])).total_seconds() for c in commits)
    failed = d["status"] != "success"
    return [span(tid, span_id(key), f"deploy {d['environment']}", at, at, {
        "deployment.environment.name": d["environment"], "deployment.id": key, "deployment.name": d["version"],
        "deployment.status": "failed" if failed else "succeeded", "vcs.ref.head.revision": d["commit_sha"],
        "greenlight.deploy.source": d["source"], "greenlight.deploy.commits": len(commits),
        "greenlight.deploy.lead_time_p50_s": leads[len(leads) // 2] if leads else None,
        "greenlight.deploy.lead_time_max_s": leads[-1] if leads else None, "url.full": d["url"],
    }, kind=INTERNAL, error="deployment failed" if failed else None)]


# ---------- metrics ----------
def gauge(name: str, unit: str, points: list[tuple[dict[str, Any], float]], now: int, desc: str = "") -> dict[str, Any]:
    return {"name": name, "unit": unit, "description": desc, "gauge": {"dataPoints": [
        {"attributes": attrs(a), "timeUnixNano": str(now), "asDouble": float(v)} for a, v in points]}}


def cumulative_sum(name: str, unit: str, points: list[tuple[dict[str, Any], float]], start: int, now: int,
                   monotonic: bool, desc: str = "") -> dict[str, Any]:
    return {"name": name, "unit": unit, "description": desc, "sum": {
        "aggregationTemporality": 2, "isMonotonic": monotonic, "dataPoints": [
            {"attributes": attrs(a), "startTimeUnixNano": str(start), "timeUnixNano": str(now), "asDouble": float(v)}
            for a, v in points]}}


DURATION_BOUNDS = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


def histogram(name: str, unit: str, series: dict[tuple, list[float]], keys: tuple[str, ...], bounds: list[float],
              start: int, now: int, desc: str = "") -> dict[str, Any]:
    points = []
    for values_key, values in series.items():
        counts = [0] * (len(bounds) + 1)
        for v in values:
            counts[next((i for i, b in enumerate(bounds) if v <= b), len(bounds))] += 1
        points.append({"attributes": attrs(dict(zip(keys, values_key))), "startTimeUnixNano": str(start),
                       "timeUnixNano": str(now), "count": str(len(values)), "sum": sum(values),
                       "bucketCounts": [str(c) for c in counts], "explicitBounds": bounds,
                       "min": min(values), "max": max(values)})
    return {"name": name, "unit": unit, "description": desc,
            "histogram": {"aggregationTemporality": 2, "dataPoints": points}}


def metrics(conn: sqlite3.Connection, repo: str | None, window_days: int = 30,
            incident_labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Cumulative over everything greenlight has (start = the oldest record), gauges over the window."""
    from . import delivery
    now = nanos(utcnow())
    first = conn.execute("SELECT MIN(created_at) FROM pipelines").fetchone()[0]
    start = nanos(first) if first else now
    out: list[dict[str, Any]] = []
    repo_url = f"https://github.com/{repo}" if repo else None

    durations: dict[tuple, list[float]] = defaultdict(list)
    errors: dict[tuple, int] = defaultdict(int)
    for p in conn.execute("SELECT workflow, status, duration_ms, queue_ms FROM pipelines WHERE finished_at IS NOT NULL"):
        result = RESULT.get(p["status"], "error")
        if p["duration_ms"] is not None:
            durations[(p["workflow"], "executing", result)].append(p["duration_ms"] / 1000)
        if p["queue_ms"] is not None:
            durations[(p["workflow"], "pending", result)].append(p["queue_ms"] / 1000)
        if result in BAD:
            errors[(p["workflow"], result)] += 1
    if durations:
        out.append(histogram("cicd.pipeline.run.duration", "s", durations,
                             ("cicd.pipeline.name", "cicd.pipeline.run.state", "cicd.pipeline.result"),
                             DURATION_BOUNDS, start, now, "Duration of a pipeline run, by state"))
    if errors:
        out.append(cumulative_sum("cicd.pipeline.run.errors", "{error}",
                                  [({"cicd.pipeline.name": w, "error.type": e}, n) for (w, e), n in errors.items()],
                                  start, now, True, "Pipeline runs that ended in failure, timeout or error"))

    states = dict(conn.execute("SELECT state, COUNT(*) FROM pull_requests GROUP BY state").fetchall())
    if states:
        out.append(cumulative_sum("vcs.change.count", "{change}", [
            ({"vcs.change.state": s, "vcs.repository.url.full": repo_url}, n) for s, n in states.items()], start, now,
            False, "Pull requests by state"))
        ages = [({"vcs.change.state": "open", "vcs.ref.head.name": r["head"], "vcs.change.id": str(r["number"]),
                  "vcs.repository.url.full": repo_url},
                 (utcnow() - parse_time(r["created_at"])).total_seconds())
                for r in conn.execute("SELECT number, head, created_at FROM pull_requests WHERE state = 'open'")]
        if ages:
            out.append(gauge("vcs.change.duration", "s", ages, now, "How long each open pull request has been open"))
        merged = [({"vcs.ref.head.name": r["head"], "vcs.change.id": str(r["number"]), "vcs.repository.url.full": repo_url},
                   (parse_time(r["merged_at"]) - parse_time(r["created_at"])).total_seconds())
                  for r in conn.execute("SELECT number, head, created_at, merged_at FROM pull_requests "
                                        "WHERE merged_at >= ? ORDER BY merged_at DESC LIMIT 200", (since(window_days),))]
        if merged:
            out.append(gauge("vcs.change.time_to_merge", "s", merged, now, "Open to merge, per pull request merged in the window"))

    w = {"greenlight.window.days": window_days}
    stats = analysis.flake_stats(conn, window_days)
    by_class: dict[str, int] = defaultdict(int)
    for s in stats.values():
        by_class[s["classification"]] += 1
    if stats:
        out.append(gauge("greenlight.tests", "{test}", [({**w, "greenlight.test.classification": c}, n)
                                                        for c, n in sorted(by_class.items())], now,
                         "Tests seen in the window, by flake classification"))
        q = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        out.append(gauge("greenlight.tests.quarantined", "{test}", [({}, q)], now, "Quarantined tests"))
        reruns = conn.execute("SELECT COUNT(*), SUM(attempt > 1) FROM runs WHERE started_at >= ?",
                              (since(window_days),)).fetchone()
        if reruns[0]:
            out.append(gauge("greenlight.runs.rerun_ratio", "1", [(w, (reruns[1] or 0) / reruns[0])], now,
                             "Share of test runs that were second or later attempts on the same code"))
    if conn.execute("SELECT 1 FROM deployments LIMIT 1").fetchone():
        d = delivery.dora(conn, window_days, incident_labels)
        m, env = d["metrics"], {**w, "deployment.environment.name": d["environment"]}
        dora = [("greenlight.dora.deployment_frequency", "{deployment}/d", m["deploys_per_day"],
                 "Successful deployments per day"),
                ("greenlight.dora.lead_time_p50", "s", m["lead_time_p50_h"] and m["lead_time_p50_h"] * 3600,
                 "Median time from commit to deployment"),
                ("greenlight.dora.lead_time_p90", "s", m["lead_time_p90_h"] and m["lead_time_p90_h"] * 3600,
                 "90th percentile time from commit to deployment"),
                ("greenlight.dora.change_failure_rate", "1", m["change_failure_rate"],
                 "Share of deployments that failed or led to an incident"),
                ("greenlight.dora.time_to_restore_p50", "s", m["time_to_restore_p50_h"] and m["time_to_restore_p50_h"] * 3600,
                 "Median time from an incident opening to it closing")]
        out += [gauge(n, u, [(env, v)], now, desc) for n, u, v, desc in dora if v is not None]
    return out


# ---------- export ----------
def _exported(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM otel_exports WHERE kind = ?", (kind,))}


def collect(conn: sqlite3.Connection, only: set[str], since_days: int, everything: bool = False,
            run_id: int | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Spans to send, and the (kind, key) pairs to mark as sent once they're accepted."""
    spans: list[dict[str, Any]] = []
    marks: list[tuple[str, str]] = []
    cutoff = since(since_days)
    if run_id is not None:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"No run {run_id}")
        classes = {k: v["classification"] for k, v in analysis.flake_stats(conn).items()}
        return run_spans(conn, run, classes), [("run", run["external_id"] or f"run:{run_id}")]
    if "pipelines" in only:
        done = set() if everything else _exported(conn, "pipeline")
        for p in conn.execute("SELECT * FROM pipelines WHERE finished_at IS NOT NULL AND created_at >= ? "
                              "ORDER BY created_at", (cutoff,)):
            if p["pipeline_id"] not in done:
                spans += pipeline_spans(conn, p)
                marks.append(("pipeline", p["pipeline_id"]))
    if "tests" in only:
        done = set() if everything else _exported(conn, "run")
        classes = {k: v["classification"] for k, v in analysis.flake_stats(conn).items()}
        for r in conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at", (cutoff,)):
            key = r["external_id"] or f"run:{r['run_id']}"
            if key not in done:
                spans += run_spans(conn, r, classes)
                marks.append(("run", key))
    if "deployments" in only:
        done = set() if everything else _exported(conn, "deployment")
        for d in conn.execute("SELECT * FROM deployments WHERE deployed_at >= ? ORDER BY deployed_at", (cutoff,)):
            if d["deploy_id"] not in done:
                spans += deployment_spans(conn, d)
                marks.append(("deployment", d["deploy_id"]))
    return spans, marks


def _batches(spans: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    """Keep every trace in one request, about BATCH spans at a time."""
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in spans:
        by_trace[s["traceId"]].append(s)
    chunk: list[dict[str, Any]] = []
    for group in by_trace.values():
        if chunk and len(chunk) + len(group) > BATCH:
            yield chunk
            chunk = []
        chunk.extend(group)
    if chunk:
        yield chunk


def export(conn: sqlite3.Connection, repo: str | None, configured_endpoint: str | None = None,
           service_name: str | None = None, only: set[str] | None = None, since_days: int = 90,
           everything: bool = False, run_id: int | None = None, dry_run: bool = False,
           window_days: int = 30, incident_labels: list[str] | None = None) -> dict[str, Any]:
    only = only or {"pipelines", "tests", "deployments", "metrics"}
    res = resource(repo, service_name)
    spans, marks = collect(conn, only, since_days, everything, run_id)
    out: dict[str, Any] = {"spans": len(spans), "traces": len({s["traceId"] for s in spans}),
                           "endpoint": endpoint("traces", configured_endpoint)}
    met = metrics(conn, repo, window_days, incident_labels) if "metrics" in only and run_id is None else []
    out["metrics"] = len(met)
    if dry_run:
        out["dry_run"] = True
        return out
    rejected = 0
    for chunk in _batches(spans):
        resp = post(endpoint("traces", configured_endpoint),
                    {"resourceSpans": [{"resource": res, "scopeSpans": [{"scope": SCOPE, "spans": chunk}]}]},
                    headers("traces"))
        rejected += int((resp.get("partialSuccess") or {}).get("rejectedSpans") or 0)
    if marks:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO otel_exports VALUES (?, ?, ?)",
                             [(k, key, iso(utcnow())) for k, key in marks])
    if met:
        resp = post(endpoint("metrics", configured_endpoint),
                    {"resourceMetrics": [{"resource": res, "scopeMetrics": [{"scope": SCOPE, "metrics": met}]}]},
                    headers("metrics"))
        out["rejected_points"] = int((resp.get("partialSuccess") or {}).get("rejectedDataPoints") or 0)
    out["rejected_spans"] = rejected
    with conn:
        conn.execute("INSERT OR REPLACE INTO sync_state (source, cursor, synced_at) VALUES ('otel export', ?, ?)",
                     (str(out["spans"]), iso(utcnow())))
    return out


# ---------- receive ----------
def _commit(a: dict[str, Any]) -> str | None:
    for k in ("vcs.ref.head.revision", "vcs.repository.ref.revision", "git.commit.sha", "ci.commit.sha"):
        if a.get(k):
            return str(a[k])
    return None


def _branch(a: dict[str, Any]) -> str | None:
    for k in ("vcs.ref.head.name", "vcs.repository.ref.name", "git.branch", "ci.branch"):
        if a.get(k):
            return str(a[k])
    return None


def _iso_from_nanos(v: Any) -> str | None:
    try:
        return iso(datetime.fromtimestamp(int(v) / 1e9, tz=timezone.utc)) if v else None
    except (TypeError, ValueError, OverflowError):
        return None


def _test_id(a: dict[str, Any], name: str) -> str:
    case = str(a.get("test.case.name") or name)
    suite = a.get("test.suite.name")
    if "::" in case or not suite or case.startswith(f"{suite}."):
        return case
    return f"{suite}::{case}"


def ingest_traces(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, int]:
    """Turn OTLP spans into greenlight rows. Test spans (test.case.name) become test results, one run
    per trace; CI/CD spans (cicd.pipeline.*) become a pipeline per trace and its jobs. Everything else is
    ignored. A span already received is skipped, so a resent batch or a re-imported file adds nothing."""
    seen_spans = _exported(conn, "received")
    tests: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    roots: dict[str, dict[str, Any]] = {}
    pipelines: list[tuple[dict, dict]] = []
    tasks: list[tuple[dict, dict]] = []
    new_marks: list[str] = []
    for rs in payload.get("resourceSpans") or []:
        res_attrs = read_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for s in ss.get("spans") or []:
                tid = str(s.get("traceId", "")).lower()
                mark = f"{tid}/{str(s.get('spanId', '')).lower()}"
                a = {**res_attrs, **read_attrs(s.get("attributes"))}
                if "test.case.name" in a:
                    if mark in seen_spans:
                        continue
                    tests[tid].append((s, a))
                elif "cicd.pipeline.task.name" in a:
                    tasks.append((s, a))
                elif "cicd.pipeline.name" in a or "cicd.pipeline.result" in a:
                    pipelines.append((s, a))
                elif "test.suite.name" in a or "greenlight.gate.decision" in a:
                    roots[tid] = a
                    continue
                else:
                    continue
                new_marks.append(mark)
    added = {"test_results": 0, "test_runs": 0, "pipelines": 0, "jobs": 0}
    for tid, items in tests.items():
        items.sort(key=lambda x: int(x[0].get("startTimeUnixNano") or 0))
        root = roots.get(tid, {})
        a0 = {**root, **items[0][1]}
        commit = a0.get("greenlight.run.code_id") or _commit(a0) or f"trace:{tid[:12]}"
        ext = f"otel:{tid}"
        row = conn.execute("SELECT run_id FROM runs WHERE external_id = ?", (ext,)).fetchone()
        count: dict[str, int] = defaultdict(int)
        if row:
            for t, n in conn.execute("SELECT test_id, COUNT(*) FROM results WHERE run_id = ? GROUP BY test_id", (row[0],)):
                count[t] = n
        results = []
        for s, a in items:
            test_id = _test_id(a, s.get("name", "test"))
            status = a.get("test.case.result.status")
            failed = status == "fail" or (status is None and (s.get("status") or {}).get("code") == ERROR)
            outcome = "fail" if failed else "skip" if a.get("greenlight.test.outcome") == "skip" else "pass"
            msg = None
            for ev in s.get("events") or []:
                if ev.get("name") == "exception":
                    msg = read_attrs(ev.get("attributes")).get("exception.message")
            msg = (msg or (s.get("status") or {}).get("message") or None) if failed else None
            start, end = int(s.get("startTimeUnixNano") or 0), int(s.get("endTimeUnixNano") or 0)
            results.append(TestResult(test_id, a.get("code.file.path"), outcome, max(0, (end - start) // 1_000_000),
                                      retry=count[test_id], failure_sig=failure_signature(msg or "failed") if failed else None,
                                      message=(msg or "")[:500] or None, flags=a.get("greenlight.test.flags")))
            count[test_id] += 1
        if row:
            with conn:
                insert_results(conn, row[0], results)
        else:
            source = a0.get("greenlight.run.source") or "otel"
            record_run(conn, results, commit_sha=str(commit), branch=_branch(a0), source=str(source), external_id=ext,
                       started_at=parse_time(_iso_from_nanos(items[0][0].get("startTimeUnixNano"))),
                       git_commit=_commit(a0), session=str(a0.get("test.suite.name") or a0.get("service.name") or "") or None,
                       url=a0.get("cicd.pipeline.run.url.full"))
            added["test_runs"] += 1
        added["test_results"] += len(results)

    with conn:
        for s, a in pipelines:
            pid = f"otel:{str(s.get('traceId', '')).lower()}"
            start, end = _iso_from_nanos(s.get("startTimeUnixNano")), _iso_from_nanos(s.get("endTimeUnixNano"))
            status = FROM_RESULT.get(str(a.get("cicd.pipeline.result")), "unknown")
            # an upsert, not a replace: replacing would cascade-delete jobs that arrived first
            conn.execute(
                """INSERT INTO pipelines (pipeline_id, provider, workflow, run_number, attempt, branch, commit_sha, status,
                   created_at, started_at, finished_at, duration_ms, url) VALUES (?, 'otel', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pipeline_id) DO UPDATE SET workflow = excluded.workflow, run_number = excluded.run_number,
                     attempt = excluded.attempt, branch = excluded.branch, commit_sha = excluded.commit_sha,
                     status = excluded.status, created_at = excluded.created_at, started_at = excluded.started_at,
                     finished_at = excluded.finished_at, duration_ms = excluded.duration_ms, url = excluded.url""",
                (pid, a.get("cicd.pipeline.name") or s.get("name"), a.get("cicd.pipeline.run.id"),
                 int(a.get("greenlight.pipeline.attempt") or 1), _branch(a), _commit(a), status, start, start,
                 end if status != "unknown" else None,
                 (int(s.get("endTimeUnixNano") or 0) - int(s.get("startTimeUnixNano") or 0)) // 1_000_000,
                 a.get("cicd.pipeline.run.url.full")))
            added["pipelines"] += 1
        for s, a in tasks:
            pid = f"otel:{str(s.get('traceId', '')).lower()}"
            start, end = _iso_from_nanos(s.get("startTimeUnixNano")), _iso_from_nanos(s.get("endTimeUnixNano"))
            # the run span usually ends, and so arrives, last: hold its place until it does
            conn.execute("INSERT OR IGNORE INTO pipelines (pipeline_id, provider, workflow, status, created_at) "
                         "VALUES (?, 'otel', 'unknown', 'in_progress', ?)", (pid, start))
            conn.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                         (f"otel:{a.get('cicd.pipeline.task.run.id') or s.get('spanId')}", pid,
                          a.get("cicd.pipeline.task.name"), FROM_RESULT.get(str(a.get("cicd.pipeline.task.run.result")), "unknown"),
                          start, end, (int(s.get("endTimeUnixNano") or 0) - int(s.get("startTimeUnixNano") or 0)) // 1_000_000,
                          a.get("cicd.worker.name"), a.get("cicd.pipeline.task.run.url.full")))
            added["jobs"] += 1
        conn.executemany("INSERT OR IGNORE INTO otel_exports VALUES ('received', ?, ?)",
                         [(m, iso(utcnow())) for m in new_marks])
    return added


def read_body(raw: bytes, encoding: str | None) -> dict[str, Any]:
    if (encoding or "").lower() == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(raw, MAX_BODY)
        if d.unconsumed_tail:
            raise ValueError("decompressed body is larger than 32 MB")
    return json.loads(raw or b"{}")


def import_file(conn: sqlite3.Connection, path: str) -> dict[str, int]:
    """A file of OTLP JSON: one export request, or one per line (the Collector's file exporter)."""
    total: dict[str, int] = defaultdict(int)
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8").strip()
    chunks = [text] if text.startswith("{") and text.count("\n{") == 0 else [line for line in text.splitlines() if line.strip()]
    for chunk in chunks:
        for k, v in ingest_traces(conn, json.loads(chunk)).items():
            total[k] += v
    return dict(total)


def make_receiver(db: str | None, token: str | None = None) -> type[BaseHTTPRequestHandler]:
    from .db import connect

    class Receiver(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            if token is not None:
                given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                if not hmac.compare_digest(given.encode(), token.encode()):
                    self._reply(401, {"message": "missing or wrong token"})
                    return
            path = urllib.parse.urlparse(self.path).path
            if path == "/v1/metrics" or path == "/v1/logs":
                self._reply(200, {})  # accepted and dropped: greenlight keeps traces only
                return
            if path != "/v1/traces":
                self._reply(404, {"message": f"no route {path}"})
                return
            if "json" not in (self.headers.get("Content-Type") or ""):
                self._reply(415, {"message": "send OTLP as JSON (otlphttp exporter with encoding: json)"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._reply(413, {"message": "body larger than 32 MB"})
                return
            try:
                payload = read_body(self.rfile.read(length), self.headers.get("Content-Encoding"))
                with closing(connect(db)) as conn:
                    ingest_traces(conn, payload)
            except (ValueError, OSError, zlib.error) as e:
                self._reply(400, {"message": str(e)})
                return
            self._reply(200, {})

        def log_message(self, *args: Any) -> None:
            pass

    return Receiver


def serve(db: str | None, host: str = "127.0.0.1", port: int = 4319, token: str | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_receiver(db, token))
    print(f"greenlight receiving OTLP/HTTP JSON on http://{host}:{port}/v1/traces  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

