"""CI pipelines, delivery (DORA) and issue analytics over the synced GitHub data.

DORA here:
  deployment frequency   successful deployments per day in the window
  lead time for changes  deployed_at minus each shipped commit's author time (median, p90)
  change failure rate    share of deployments that failed, or that an incident issue (opened
                         after it and before the next deployment) points back to
  time to restore        an incident issue's open-to-close time (median)
Incidents are issues carrying one of the configured incident labels.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from .db import day_range, parse_time, since, utcnow

SUCCESS = "success"
FAILED = ("failure", "timed_out", "startup_failure")


def pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, q in 0..1."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, math.ceil(q * len(vals)) - 1))]


def _hours(a: str | None, b: str | None) -> float | None:
    x, y = parse_time(a), parse_time(b)
    return None if not x or not y else max(0.0, (y - x).total_seconds() / 3600)


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


# ---------- pipelines ----------
def pipelines_summary(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    start = since(days)
    rows = [dict(r) for r in conn.execute("SELECT * FROM pipelines WHERE created_at >= ? ORDER BY created_at", (start,))]
    day_list = day_range(days)
    ok_per_day, bad_per_day = Counter(), Counter()
    by_workflow: dict[str, list[dict]] = defaultdict(list)
    runs_latest: dict[str, dict] = {}
    for r in rows:
        by_workflow[r["workflow"]].append(r)
        run_key = r["pipeline_id"].rsplit(":", 1)[0]
        if run_key not in runs_latest or r["attempt"] > runs_latest[run_key]["attempt"]:
            runs_latest[run_key] = r
    for r in runs_latest.values():
        if r["status"] == SUCCESS:
            ok_per_day[_day(r["created_at"])] += 1
        elif r["status"] in FAILED:
            bad_per_day[_day(r["created_at"])] += 1

    workflows = []
    for name, rs in sorted(by_workflow.items()):
        latest = [r for k, r in runs_latest.items() if r["workflow"] == name]
        done = [r for r in latest if r["status"] in (SUCCESS, *FAILED)]
        durations = [r["duration_ms"] for r in rs if r["duration_ms"] is not None and r["status"] in (SUCCESS, *FAILED)]
        queues = [r["queue_ms"] for r in rs if r["queue_ms"] is not None]
        reran = {r["pipeline_id"].rsplit(":", 1)[0] for r in rs if r["attempt"] > 1}
        last = max(latest, key=lambda r: r["created_at"]) if latest else None
        workflows.append({
            "workflow": name,
            "runs": len(latest),
            "attempts": len(rs),
            "success_rate": round(sum(r["status"] == SUCCESS for r in done) / len(done), 4) if done else None,
            "rerun_runs": len(reran),
            "p50_duration_ms": pct(durations, .5),
            "p95_duration_ms": pct(durations, .95),
            "p50_queue_ms": pct(queues, .5),
            "last_status": last["status"] if last else None,
            "last_at": last["created_at"] if last else None,
            "durations": [r["duration_ms"] for r in rs[-30:] if r["duration_ms"] is not None],
        })

    return {
        "window_days": days,
        "days": day_list,
        "success_per_day": [ok_per_day.get(d, 0) for d in day_list],
        "failed_per_day": [bad_per_day.get(d, 0) for d in day_list],
        "workflows": workflows,
        "flaky_jobs": flaky_jobs(conn, days),
        "slow_jobs": slow_jobs(conn, days),
        "recent": [_pipeline_row(r) for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:50]],
        "totals": {
            "runs": len(runs_latest),
            "failed": sum(r["status"] in FAILED for r in runs_latest.values()),
            "rerun": len({k for k, r in runs_latest.items() if r["attempt"] > 1}),
            "minutes": round(sum(r["duration_ms"] or 0 for r in rows) / 60000, 1),
        },
    }


def _pipeline_row(r: dict) -> dict:
    return {k: r[k] for k in ("pipeline_id", "workflow", "run_number", "attempt", "event", "branch", "commit_sha",
                              "status", "created_at", "duration_ms", "queue_ms", "actor", "url")}


def flaky_jobs(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """A job that both failed and succeeded on the same commit (a rerun attempt, or another run of the
    same workflow) flipped, the same rule tests follow."""
    rows = conn.execute(
        """WITH per_sha AS (
               SELECT p.workflow, j.name, p.commit_sha,
                      COUNT(*) AS execs,
                      SUM(j.status = 'success') AS ok,
                      SUM(j.status IN ('failure', 'timed_out')) AS bad,
                      MAX(CASE WHEN j.status IN ('failure', 'timed_out') THEN p.created_at END) AS last_fail
               FROM jobs j JOIN pipelines p ON p.pipeline_id = j.pipeline_id
               WHERE p.created_at >= ? AND p.commit_sha IS NOT NULL
               GROUP BY p.workflow, j.name, p.commit_sha)
           SELECT workflow, name, SUM(execs >= 2) AS eligible, SUM(ok > 0 AND bad > 0) AS flips,
                  SUM(bad) AS failures, MAX(CASE WHEN ok > 0 AND bad > 0 THEN last_fail END) AS last_flip_at
           FROM per_sha GROUP BY workflow, name HAVING flips > 0
           ORDER BY flips DESC, failures DESC""", (since(days),)).fetchall()
    return [dict(r) for r in rows]


def slow_jobs(conn: sqlite3.Connection, days: int = 30, limit: int = 8) -> list[dict[str, Any]]:
    per: dict[tuple[str, str], list[int]] = defaultdict(list)
    for wf, name, ms in conn.execute(
            """SELECT p.workflow, j.name, j.duration_ms FROM jobs j JOIN pipelines p ON p.pipeline_id = j.pipeline_id
               WHERE p.created_at >= ? AND j.duration_ms IS NOT NULL AND j.status = 'success'""", (since(days),)):
        per[(wf, name)].append(ms)
    out = [{"workflow": wf, "job": name, "runs": len(v), "p50_ms": pct(v, .5), "p95_ms": pct(v, .95)}
           for (wf, name), v in per.items()]
    return sorted(out, key=lambda r: r["p50_ms"] or 0, reverse=True)[:limit]


def pipeline_detail(conn: sqlite3.Connection, pipeline_id: str) -> dict[str, Any]:
    p = conn.execute("SELECT * FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    if p is None:
        raise LookupError(f"No pipeline {pipeline_id}. Run `greenlight sync` to pull Actions runs.")
    p = dict(p)
    origin = parse_time(p["started_at"] or p["created_at"])
    jobs = []
    for j in conn.execute("SELECT * FROM jobs WHERE pipeline_id = ? ORDER BY started_at, name", (pipeline_id,)):
        j = dict(j)
        st = parse_time(j["started_at"])
        j["offset_ms"] = int((st - origin).total_seconds() * 1000) if st and origin else None
        j["steps"] = [dict(s) for s in conn.execute("SELECT * FROM steps WHERE job_id = ? ORDER BY number", (j["job_id"],))]
        jobs.append(j)
    siblings = [dict(r) for r in conn.execute(
        "SELECT pipeline_id, attempt, status, created_at FROM pipelines WHERE pipeline_id LIKE ? ORDER BY attempt",
        (p["pipeline_id"].rsplit(":", 1)[0] + ":%",))]
    tests = [dict(r) for r in conn.execute(
        "SELECT run_id, external_id, attempt FROM runs WHERE commit_sha = ? AND source = 'ci' ORDER BY started_at",
        (p["commit_sha"],))] if p["commit_sha"] else []
    return {"pipeline": p, "jobs": jobs, "attempts": siblings, "test_runs": tests}


# ---------- delivery ----------
def _incidents(conn: sqlite3.Connection, labels: list[str]) -> list[dict[str, Any]]:
    want = set(labels)
    out = []
    for r in conn.execute("SELECT * FROM issues ORDER BY created_at"):
        if want & set(json.loads(r["labels"] or "[]")):
            out.append(dict(r))
    return out


def pick_environment(conn: sqlite3.Connection, environment: str | None = None) -> str | None:
    if environment:
        return environment
    row = conn.execute("SELECT environment, COUNT(*) AS n FROM deployments GROUP BY environment "
                       "ORDER BY n DESC, environment LIMIT 1").fetchone()
    return row["environment"] if row else None


def dora(conn: sqlite3.Connection, days: int = 30, incident_labels: list[str] | None = None,
         environment: str | None = None) -> dict[str, Any]:
    labels = incident_labels or ["incident", "hotfix", "bug"]
    env = pick_environment(conn, environment)
    environments = [dict(r) for r in conn.execute(
        "SELECT environment, COUNT(*) AS deployments, MAX(deployed_at) AS last_at FROM deployments "
        "GROUP BY environment ORDER BY deployments DESC")]
    start = since(days)
    all_deploys = [dict(r) for r in conn.execute(
        "SELECT * FROM deployments WHERE environment = ? ORDER BY deployed_at", (env,))] if env else []
    incidents = _incidents(conn, labels)

    # each incident points back at the last deployment before it opened
    caused: dict[str, list[int]] = defaultdict(list)
    ok_deploys = [d for d in all_deploys if d["status"] == SUCCESS]
    for inc in incidents:
        before = [d for d in ok_deploys if d["deployed_at"] <= inc["created_at"]]
        if before:
            caused[before[-1]["deploy_id"]].append(inc["number"])

    window = [d for d in all_deploys if d["deployed_at"] >= start and d["status"] in (SUCCESS, "failure")]
    lead: dict[str, list[float]] = defaultdict(list)
    for deploy_id, authored, deployed in conn.execute(
            """SELECT dc.deploy_id, dc.authored_at, d.deployed_at FROM deploy_commits dc
               JOIN deployments d ON d.deploy_id = dc.deploy_id
               WHERE d.environment = ? AND d.deployed_at >= ?""", (env, start)):
        h = _hours(authored, deployed)
        if h is not None:
            lead[deploy_id].append(h)

    day_list = day_range(days)
    per_day = Counter(_day(d["deployed_at"]) for d in window if d["status"] == SUCCESS)
    rows = []
    for d in window:
        lt = lead.get(d["deploy_id"], [])
        failed = d["status"] != SUCCESS or bool(caused.get(d["deploy_id"]))
        rows.append({**{k: d[k] for k in ("deploy_id", "source", "environment", "commit_sha", "deployed_at", "status",
                                          "version", "url")},
                     "commits": len(lt), "lead_time_p50_h": pct(lt, .5), "failed": failed,
                     "incidents": caused.get(d["deploy_id"], [])})
    all_lead = [h for d in window for h in lead.get(d["deploy_id"], [])]
    restored = [_hours(i["created_at"], i["closed_at"]) for i in incidents
                if i["created_at"] >= start and i["closed_at"]]
    succeeded = [d for d in window if d["status"] == SUCCESS]
    first = parse_time(all_deploys[0]["deployed_at"]) if all_deploys else None
    span_days = min(days, max(1.0, (utcnow() - first).total_seconds() / 86400)) if first else days
    return {
        "window_days": days,
        "environment": env,
        "environments": environments,
        "incident_labels": labels,
        "metrics": {
            "deployments": len(succeeded),
            "deploys_per_day": round(len(succeeded) / span_days, 2) if succeeded else 0.0,
            "deploy_days": sum(1 for d in day_list if per_day.get(d)),
            "lead_time_p50_h": pct(all_lead, .5),
            "lead_time_p90_h": pct(all_lead, .9),
            "change_failure_rate": round(sum(r["failed"] for r in rows) / len(rows), 4) if rows else None,
            "failed_deployments": sum(r["failed"] for r in rows),
            "time_to_restore_p50_h": pct([h for h in restored if h is not None], .5),
            "incidents": sum(1 for i in incidents if i["created_at"] >= start),
            "open_incidents": sum(1 for i in incidents if i["state"] == "open"),
        },
        "days": day_list,
        "deploys_per_day": [per_day.get(d, 0) for d in day_list],
        "lead_time_per_day": _lead_by_day(rows, day_list),
        "deployments": sorted(rows, key=lambda r: r["deployed_at"], reverse=True),
    }


def _lead_by_day(rows: list[dict], day_list: list[str]) -> list[float | None]:
    per: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["lead_time_p50_h"] is not None:
            per[_day(r["deployed_at"])].append(r["lead_time_p50_h"])
    return [round(pct(per[d], .5), 2) if per.get(d) else None for d in day_list]


def pr_flow(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    start = since(days)
    prs = [dict(r) for r in conn.execute("SELECT * FROM pull_requests WHERE created_at >= ? OR state = 'open'", (start,))]
    merged = [p for p in prs if p["merged_at"] and p["merged_at"] >= start]
    day_list = day_range(days)
    per_day = Counter(_day(p["merged_at"]) for p in merged)
    to_merge = [_hours(p["created_at"], p["merged_at"]) for p in merged]
    open_prs = sorted((p for p in prs if p["state"] == "open"), key=lambda p: p["created_at"])
    now = utcnow().isoformat()
    return {
        "window_days": days,
        "days": day_list,
        "merged_per_day": [per_day.get(d, 0) for d in day_list],
        "metrics": {
            "merged": len(merged),
            "opened": sum(1 for p in prs if p["created_at"] >= start),
            "closed_unmerged": sum(1 for p in prs if p["state"] == "closed" and (p["closed_at"] or "") >= start),
            "open": len(open_prs),
            "time_to_merge_p50_h": pct(to_merge, .5),
            "time_to_merge_p90_h": pct(to_merge, .9),
        },
        "open": [{**{k: p[k] for k in ("number", "title", "author", "base", "head", "created_at", "draft", "url")},
                  "age_h": _hours(p["created_at"], now)} for p in open_prs[:25]],
    }


def issues_summary(conn: sqlite3.Connection, days: int = 30, incident_labels: list[str] | None = None) -> dict[str, Any]:
    start = since(days)
    issues = [dict(r) for r in conn.execute("SELECT * FROM issues")]
    for i in issues:
        i["labels"] = json.loads(i["labels"] or "[]")
    day_list = day_range(days)
    opened = Counter(_day(i["created_at"]) for i in issues if i["created_at"] >= start)
    # the backlog at the end of each day: opened by then and not yet closed
    open_per_day = [sum(1 for i in issues if _day(i["created_at"]) <= day and (not i["closed_at"] or _day(i["closed_at"]) > day))
                    for day in day_list]
    closed = Counter(_day(i["closed_at"]) for i in issues if i["closed_at"] and i["closed_at"] >= start)
    open_issues = [i for i in issues if i["state"] == "open"]
    now = utcnow().isoformat()
    by_label = Counter(lb for i in open_issues for lb in i["labels"])
    managed = sorted((i for i in issues if i["managed_key"]), key=lambda i: (i["state"] != "open", i["created_at"]))
    close_times = [_hours(i["created_at"], i["closed_at"]) for i in issues if i["closed_at"] and i["closed_at"] >= start]
    return {
        "window_days": days,
        "days": day_list,
        "opened_per_day": [opened.get(d, 0) for d in day_list],
        "closed_per_day": [closed.get(d, 0) for d in day_list],
        "open_per_day": open_per_day,
        "metrics": {
            "open": len(open_issues),
            "opened": sum(opened.values()),
            "closed": sum(closed.values()),
            "time_to_close_p50_h": pct(close_times, .5),
            "open_age_p50_h": pct([_hours(i["created_at"], now) for i in open_issues], .5),
        },
        "open_by_label": by_label.most_common(12),
        "managed": [{**{k: i[k] for k in ("number", "title", "state", "managed_key", "created_at", "closed_at", "url")},
                     "kind": i["managed_key"].split(":", 1)[0], "test_id": i["managed_key"].split(":", 1)[1]} for i in managed],
        "oldest_open": [{**{k: i[k] for k in ("number", "title", "labels", "created_at", "url")},
                         "age_h": _hours(i["created_at"], now)}
                        for i in sorted(open_issues, key=lambda i: i["created_at"])[:15]],
        "incident_labels": incident_labels or [],
    }


def delivery_view(conn: sqlite3.Connection, days: int = 30, incident_labels: list[str] | None = None,
                  environment: str | None = None) -> dict[str, Any]:
    return {"dora": dora(conn, days, incident_labels, environment), "prs": pr_flow(conn, days),
            "issues": issues_summary(conn, days, incident_labels),
            "synced": {r["source"]: r["synced_at"] for r in conn.execute("SELECT source, synced_at FROM sync_state")}}


def test_metrics(conn: sqlite3.Connection, test_id: str, limit: int = 60) -> dict[str, Any]:
    """A test's measured numbers per run, oldest first: [{run_id, started_at, commit, value_ms, base_ms...}]."""
    rows = conn.execute(
        """SELECT m.run_id, ru.started_at, COALESCE(ru.git_commit, ru.commit_sha) AS sha, m.name, m.value
           FROM metrics m JOIN runs ru ON ru.run_id = m.run_id
           WHERE m.test_id = ? ORDER BY ru.started_at DESC, m.run_id DESC""", (test_id,)).fetchall()
    per: dict[int, dict[str, Any]] = {}
    for r in rows:
        d = per.setdefault(r["run_id"], {"run_id": r["run_id"], "started_at": r["started_at"], "sha": r["sha"]})
        d[r["name"]] = r["value"]
    points = sorted(per.values(), key=lambda d: d["started_at"])[-limit:]
    return {"test_id": test_id, "points": points, "names": sorted({r["name"] for r in rows})}
