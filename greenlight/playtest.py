"""Adapter for a playtest ledger: run records committed to git, like survive-project's
tools/playtest/runs/<time>-<commit>.json (see its tools/playtest/ledger.cjs).

A record names the commit a run started on, the files that were uncommitted then (by content
hash), and per suite whether it passed, how many checks ran, and which failed or ran slower than
their base build. It does not name the checks that passed. So for each suite we keep a "watch set",
every check that has ever failed or run slower in any record, and in each run where that suite ran
to the end, a watched check that is not listed as failed counts as an inferred pass. On the same
code that inference is exact, which is all flake detection needs.

The runner's full report (tools/playtest/out/report.json) names every check with its detail and,
for perf, its numbers. When it is present it upgrades the matching record's run in place.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import iso, parse_time, utcnow
from .gitrepo import Repo, code_identity
from .ingest import TestResult, failure_signature, insert_results, record_run, MESSAGE_LIMIT

RUNS_DIR = "tools/playtest/runs"
REPORT = "tools/playtest/out/report.json"
KNOWN = "tools/playtest/known-failures.json"
CRASH_CHECK = "the suite runs to the end"
SOURCE_RECORD, SOURCE_REPORT = "playtest", "playtest-report"
KNOWN_BY = "known-failures.json"
DEFAULT_BRANCHES = {"main", "master", "release", "HEAD"}


@dataclass
class Record:
    name: str
    data: dict[str, Any]
    branches: set[str] = field(default_factory=set)
    in_checkout: bool = False

    @property
    def commit(self) -> str:
        return self.data.get("commit") or ""

    @property
    def code_id(self) -> str:
        return code_identity(self.commit, self.data.get("changed") or {})

    @property
    def branch(self) -> str | None:
        """main once merged (work branches that merged main also carry it), else the work branch."""
        for b in ("main", "master"):
            if b in self.branches:
                return b
        own = sorted(b for b in self.branches if b not in DEFAULT_BRANCHES)
        return own[0] if own else ("release" if "release" in self.branches else None)




def external_id(name: str) -> str:
    return f"playtest:{name}"


def record_name(when: str, commit: str) -> str:
    """ledger.cjs: `${report.when.replace(/[-:]/g, '').slice(0, 15)}-${commit.slice(0, 7)}.json`."""
    return f"{when.replace('-', '').replace(':', '')[:15]}-{commit[:7]}.json"


def read_records(repo: Repo, runs_dir: str = RUNS_DIR) -> list[Record]:
    """Every record in the checkout and on every local or remote-tracking branch, oldest first.
    Files are deduplicated by name, like ledger.cjs."""
    found: dict[str, Record] = {}
    local = Path(repo.path) / runs_dir
    if local.is_dir():
        for f in sorted(local.glob("*.json")):
            try:
                found[f.name] = Record(f.name, json.loads(f.read_text(encoding="utf-8")), in_checkout=True)
            except (ValueError, OSError):
                continue

    blob_of: dict[str, str] = {}
    for ref, branch in repo.branch_refs():
        for name, blob in repo.ls_dir(ref, runs_dir).items():
            if not name.endswith(".json"):
                continue
            blob_of.setdefault(name, blob)
            if name in found:
                found[name].branches.add(branch)
            else:
                found[name] = Record(name, {}, {branch})
    wanted = {blob_of[n] for n, r in found.items() if not r.data and n in blob_of}
    blobs = repo.read_blobs(sorted(wanted))
    for name, rec in found.items():
        if not rec.data and name in blob_of:
            try:
                rec.data = json.loads(blobs.get(blob_of[name], b"") or b"{}")
            except ValueError:
                rec.data = {}
    usable = [r for r in found.values() if r.data.get("when") and r.commit and isinstance(r.data.get("suites"), dict)]
    return sorted(usable, key=lambda r: (r.data["when"], r.name))


def watch_set(records: list[Record]) -> dict[str, set[str]]:
    """suite -> every check that failed or ran slower in any record."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in records:
        for suite, v in r.data["suites"].items():
            if isinstance(v, dict):
                out[suite].update(v.get("failed") or [])
                out[suite].update(v.get("slower") or [])
    return out


def _crashed(v: dict[str, Any]) -> bool:
    return CRASH_CHECK in (v.get("failed") or [])


def record_results(rec: Record, watch: dict[str, set[str]]) -> list[TestResult]:
    out: list[TestResult] = []
    for suite, v in rec.data["suites"].items():
        if not isinstance(v, dict):
            continue
        failed = list(dict.fromkeys(v.get("failed") or []))
        slower = [s for s in dict.fromkeys(v.get("slower") or []) if s not in failed]
        for name in failed:
            out.append(TestResult(f"{suite}::{name}", None, "fail", None))
        for name in slower:
            out.append(TestResult(f"{suite}::{name}", None, "pass", None, flags="slower"))
        out.extend(_inferred(suite, v, watch, set(failed) | set(slower)))
    return out


def _inferred(suite: str, v: dict[str, Any], watch: dict[str, set[str]], listed: set[str]) -> list[TestResult]:
    if _crashed(v):  # checks after the crash never ran, so none of them passed
        return []
    return [TestResult(f"{suite}::{name}", None, "pass", None, flags="inferred")
            for name in sorted(watch.get(suite, set()) - listed)]


def record_metrics(rec: Record) -> list[tuple[str, str, float]]:
    """Optional numbers a record may carry: per-suite `secs`, and perf `values` {check: [value, base]}."""
    out = []
    for suite, v in rec.data["suites"].items():
        if not isinstance(v, dict):
            continue
        if isinstance(v.get("secs"), (int, float)):
            out.append((suite, "duration_ms", float(v["secs"]) * 1000))
        for name, pair in (v.get("values") or {}).items():
            if isinstance(pair, (list, tuple)) and pair and isinstance(pair[0], (int, float)):
                out.append((f"{suite}::{name}", "value_ms", float(pair[0])))
                if len(pair) > 1 and isinstance(pair[1], (int, float)):
                    out.append((f"{suite}::{name}", "base_ms", float(pair[1])))
    return out


def _total_checks(rec: Record) -> int | None:
    n = sum(v.get("checks") or 0 for v in rec.data["suites"].values() if isinstance(v, dict))
    return n or None


def _session_url(session: str | None) -> str | None:
    return f"https://claude.ai/code/{session}" if session and session.startswith("session_") else None


def sync_records(conn: sqlite3.Connection, repo: Repo, runs_dir: str = RUNS_DIR) -> dict[str, Any]:
    records = read_records(repo, runs_dir)
    watch = watch_set(records)
    existing = {r["external_id"]: (r["run_id"], r["source"]) for r in conn.execute(
        "SELECT run_id, external_id, source FROM runs WHERE external_id LIKE 'playtest:%'")}
    new_runs = inferred_added = 0
    for rec in records:
        ext = external_id(rec.name)
        if ext in existing:
            run_id, source = existing[ext]
            if source == SOURCE_RECORD:  # a report-upgraded run already names every check
                rows = [r for suite, v in rec.data["suites"].items() if isinstance(v, dict)
                        for r in _inferred(suite, v, watch, set(v.get("failed") or []) | set(v.get("slower") or []))]
                with conn:
                    inferred_added += insert_results(conn, run_id, rows, replace=False)
            continue
        session = rec.data.get("session") or None
        record_run(
            conn, record_results(rec, watch), commit_sha=rec.code_id, branch=rec.branch, source=SOURCE_RECORD,
            external_id=ext, started_at=parse_time(rec.data["when"]), git_commit=rec.commit,
            total_tests=_total_checks(rec), session=session, command=rec.data.get("command"),
            url=_session_url(session), metrics=record_metrics(rec))
        new_runs += 1
    return {"records": len(records), "new_runs": new_runs, "inferred_results_added": max(inferred_added, 0),
            "watched_checks": sum(len(v) for v in watch.values())}


def sync_known_failures(conn: sqlite3.Connection, repo: Repo, path: str = KNOWN) -> int:
    """Mirror known-failures.json on the default branch into quarantine. Only rows this mirror added
    are ever removed, so manual quarantines are left alone."""
    text = repo.show(repo.default_branch(), path)
    try:
        known = json.loads(text) if text else {}
    except ValueError:
        known = {}
    wanted = {}
    for key, why in (known.items() if isinstance(known, dict) else []):
        suite, sep, check = str(key).partition(": ")
        if sep:
            wanted[f"{suite}::{check}"] = str(why)
    with conn:
        conn.execute(f"DELETE FROM quarantine WHERE added_by = ? AND test_id NOT IN ({','.join('?' * len(wanted))})",
                     [KNOWN_BY, *wanted])
        for test_id, why in wanted.items():
            conn.execute("INSERT INTO quarantine (test_id, reason, added_at, added_by) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(test_id) DO UPDATE SET reason = excluded.reason "
                         "WHERE quarantine.added_by = excluded.added_by",
                         (test_id, f"known failure: {why}", iso(utcnow()), KNOWN_BY))
    return len(wanted)


def report_results(report: dict[str, Any]) -> tuple[list[TestResult], list[tuple[str, str, float]], int]:
    """Every check in run.cjs's report.json, plus metrics. Returns (results, metrics, duration_ms)."""
    results: list[TestResult] = []
    metrics: list[tuple[str, str, float]] = []
    total_secs = 0.0
    for s in report.get("suites") or []:
        suite = s.get("suite")
        if not suite:
            continue
        if isinstance(s.get("secs"), (int, float)):
            total_secs += s["secs"]
            metrics.append((suite, "duration_ms", float(s["secs"]) * 1000))
        for c in s.get("checks") or []:
            test_id = f"{suite}::{c.get('name')}"
            flags = [f for f in ("known", "slower") if c.get(f)]
            failed = not c.get("ok") and not c.get("slower")
            detail = (c.get("detail") or "")[:MESSAGE_LIMIT] or None
            results.append(TestResult(
                test_id, None, "fail" if failed else "pass", None,
                failure_sig=failure_signature(detail or "failed") if failed else None,
                message=detail if failed or c.get("slower") else None,
                flags=",".join(flags) or None))
            for key, name in (("value", "value_ms"), ("base", "base_ms"), ("budget", "budget_ms")):
                if isinstance(c.get(key), (int, float)):
                    metrics.append((test_id, name, float(c[key])))
    # a check listed twice (perf runs several times) keeps its last row
    unique = {r.test_id: r for r in results}
    return list(unique.values()), metrics, int(total_secs * 1000)


def ingest_report(conn: sqlite3.Connection, repo: Repo, report_path: str | None = None,
                  runs_dir: str = RUNS_DIR) -> dict[str, Any]:
    """Upgrade the run of the newest report.json with every check it names. The report carries only a
    short commit, so the matching record (same file name ledger.cjs gives it) supplies the rest."""
    path = Path(report_path) if report_path else Path(repo.path) / REPORT
    if not path.is_file():
        return {"report": None}
    report = json.loads(path.read_text(encoding="utf-8"))
    name = record_name(report.get("when", ""), report.get("commit", ""))
    rec_path = Path(repo.path) / runs_dir / name
    if not rec_path.is_file():
        return {"report": str(path), "upgraded": False,
                "why": f"no record {name}: runs with --no-build or PLAYTEST_URL are not recorded"}
    rec = Record(name, json.loads(rec_path.read_text(encoding="utf-8")), in_checkout=True)
    results, metrics, duration = report_results(report)
    ext = external_id(name)
    row = conn.execute("SELECT run_id, source FROM runs WHERE external_id = ?", (ext,)).fetchone()
    session = rec.data.get("session") or None
    if row is None:
        record_run(conn, results, commit_sha=rec.code_id, branch=None, source=SOURCE_REPORT, external_id=ext,
                   started_at=parse_time(rec.data["when"]), duration_ms=duration or None, git_commit=rec.commit,
                   session=session, command=rec.data.get("command"), url=_session_url(session), metrics=metrics)
        return {"report": str(path), "upgraded": True, "record": name, "checks": len(results)}
    if row["source"] == SOURCE_REPORT:
        return {"report": str(path), "upgraded": False, "record": name, "why": "already ingested"}
    with conn:
        conn.execute("DELETE FROM results WHERE run_id = ?", (row["run_id"],))
        conn.execute("DELETE FROM metrics WHERE run_id = ?", (row["run_id"],))
        insert_results(conn, row["run_id"], results)
        conn.executemany("INSERT OR REPLACE INTO metrics (run_id, test_id, name, value) VALUES (?, ?, ?, ?)",
                         [(row["run_id"], t, n, v) for t, n, v in metrics])
        conn.execute("UPDATE runs SET source = ?, total_tests = NULL, duration_ms = COALESCE(?, duration_ms) "
                     "WHERE run_id = ?", (SOURCE_REPORT, duration or None, row["run_id"]))
    return {"report": str(path), "upgraded": True, "record": name, "checks": len(results)}


def sync(conn: sqlite3.Connection, repo_path: str, runs_dir: str = RUNS_DIR, known_path: str = KNOWN,
         report_path: str | None = None) -> dict[str, Any]:
    repo = Repo(repo_path)
    if not repo.ok():
        raise ValueError(f"{repo_path} is not a git checkout. Point [playtest] repo (or --repo) at your clone.")
    out = sync_records(conn, repo, runs_dir)
    out["report"] = ingest_report(conn, repo, report_path, runs_dir)
    out["known_failures"] = sync_known_failures(conn, repo, known_path)
    with conn:
        conn.execute("INSERT OR REPLACE INTO sync_state (source, cursor, synced_at) VALUES ('playtest', ?, ?)",
                     (str(out["records"]), iso(utcnow())))
    return out


def latest_local_run(conn: sqlite3.Connection, repo_path: str, runs_dir: str = RUNS_DIR,
                     report_path: str | None = None) -> int | None:
    """run_id of the run just made in this checkout: the one report.json (never committed) belongs
    to, else the newest record in the runs folder."""
    local = Path(repo_path) / runs_dir
    names = sorted(f.name for f in local.glob("*.json")) if local.is_dir() else []
    report = Path(report_path) if report_path else Path(repo_path) / REPORT
    if report.is_file():
        try:
            r = json.loads(report.read_text(encoding="utf-8"))
            names.append(record_name(r.get("when", ""), r.get("commit", "")))
        except ValueError:
            pass
    for name in reversed(names):
        row = conn.execute("SELECT run_id FROM runs WHERE external_id = ?", (external_id(name),)).fetchone()
        if row:
            return row["run_id"]
    return None


def rerun_command(test_ids: list[str]) -> str | None:
    suites = sorted({t.split("::", 1)[0] for t in test_ids})
    return f"node tools/playtest/run.cjs {' '.join(suites)} --rerun" if suites else None
