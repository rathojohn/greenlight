"""Queries that feed the web UI. Everything returns plain JSON-able dicts."""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from typing import Any

from . import analysis
from .db import day_range, since

FIELD_TESTS = 12
FIELD_COMMITS = 40
STRIP_LENGTH = 30


def _placeholders(items: list) -> str:
    return ",".join("?" * len(items))


def latest_verdict(conn: sqlite3.Connection, window_days: int) -> dict[str, Any] | None:
    try:
        return analysis.triage_run(conn, window_days=window_days)
    except LookupError:
        return None


def overview(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    day_list = day_range(days)
    start = since(days)

    per_day = {r[0]: r for r in conn.execute(
        """SELECT substr(started_at, 1, 10) AS day,
                  SUM(attempt = 1), SUM(attempt > 1),
                  SUM(CASE WHEN attempt > 1 THEN COALESCE(duration_ms, 0) ELSE 0 END)
           FROM runs WHERE started_at >= ? GROUP BY day""", (start,))}
    fail_rate = dict(conn.execute(
        """SELECT substr(ru.started_at, 1, 10), 1.0 * SUM(r.outcome IN ('fail','error')) / COUNT(*)
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE ru.started_at >= ? AND r.outcome != 'skip' GROUP BY 1""", (start,)).fetchall())

    first = [int(per_day[d][1]) if d in per_day else 0 for d in day_list]
    reruns = [int(per_day[d][2]) if d in per_day else 0 for d in day_list]
    rerun_ms = sum(int(per_day[d][3] or 0) for d in day_list if d in per_day)

    stats = analysis.flake_stats(conn, days)
    counts: dict[str, int] = defaultdict(int)
    for s in stats.values():
        counts[s["classification"]] += 1
    quarantined = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]

    return {
        "days": day_list,
        "runs_first": first,
        "runs_rerun": reruns,
        "failure_rate": [round(fail_rate[d], 4) if d in fail_rate else None for d in day_list],
        "totals": {
            "runs": sum(first) + sum(reruns),
            "reruns": sum(reruns),
            "rerun_ms": rerun_ms,
            "flaky": counts["flaky"],
            "suspect": counts["suspect"],
            "failing": counts["failing"],
            "tests": len(stats),
            "quarantined": quarantined,
        },
        "latest": latest_verdict(conn, days),
        "field": flake_field(conn, days, stats),
        "synced": {r["source"]: r["synced_at"] for r in conn.execute("SELECT source, synced_at FROM sync_state")},
        "has": {
            "pipelines": bool(conn.execute("SELECT 1 FROM pipelines LIMIT 1").fetchone()),
            "deployments": bool(conn.execute("SELECT 1 FROM deployments LIMIT 1").fetchone()),
            "pull_requests": bool(conn.execute("SELECT 1 FROM pull_requests LIMIT 1").fetchone()),
            "issues": bool(conn.execute("SELECT 1 FROM issues LIMIT 1").fetchone()),
        },
    }


def flake_field(conn: sqlite3.Connection, days: int, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Most unstable tests by recent commits. Each cell is [passes, fails] or None if not run."""
    ranked = sorted(
        (s for s in stats.values() if s["classification"] in ("flaky", "suspect", "failing")),
        key=lambda s: (s["flake_score"], s["flip_shas"], s["failures"]), reverse=True)[:FIELD_TESTS]
    commits = [dict(r) for r in conn.execute(
        """SELECT commit_sha AS sha, MIN(started_at) AS first_at, COUNT(*) AS runs
           FROM runs WHERE started_at >= ? GROUP BY commit_sha ORDER BY first_at DESC LIMIT ?""",
        (since(days), FIELD_COMMITS))][::-1]
    if not ranked or not commits:
        return {"commits": commits, "tests": []}

    shas = [c["sha"] for c in commits]
    ids = [s["test_id"] for s in ranked]
    cells: dict[tuple[str, str], list[int]] = {}
    for t, sha, p, f in conn.execute(
        f"""SELECT r.test_id, ru.commit_sha, SUM(r.outcome = 'pass'), SUM(r.outcome IN ('fail','error'))
            FROM results r JOIN runs ru ON ru.run_id = r.run_id
            WHERE ru.commit_sha IN ({_placeholders(shas)}) AND r.test_id IN ({_placeholders(ids)})
              AND r.outcome != 'skip'
            GROUP BY r.test_id, ru.commit_sha""", [*shas, *ids]):
        cells[(t, sha)] = [int(p), int(f)]

    return {
        "commits": commits,
        "tests": [{
            "test_id": s["test_id"],
            "classification": s["classification"],
            "flip_shas": s["flip_shas"],
            "eligible_shas": s["eligible_shas"],
            "quarantined": s["quarantined"],
            "cells": [cells.get((s["test_id"], sha)) for sha in shas],
        } for s in ranked],
    }


def recent_outcomes(conn: sqlite3.Connection, test_ids: list[str], n: int = STRIP_LENGTH) -> dict[str, list[str]]:
    """Last n executions per test, oldest first, as 'p' / 'f' (skips dropped)."""
    if not test_ids:
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    for t, o in conn.execute(
        f"""SELECT test_id, outcome FROM (
                SELECT r.test_id, r.outcome,
                       ROW_NUMBER() OVER (PARTITION BY r.test_id
                                          ORDER BY ru.started_at DESC, ru.run_id DESC, r.retry DESC) AS rn
                FROM results r JOIN runs ru ON ru.run_id = r.run_id
                WHERE r.test_id IN ({_placeholders(test_ids)}) AND r.outcome != 'skip')
            WHERE rn <= ? ORDER BY test_id, rn DESC""", [*test_ids, n]):
        out[t].append("p" if o == "pass" else "f")
    return dict(out)


def flaky_table(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    rows = analysis.list_flaky(conn, days, min_flips=1, include_quarantined=True, limit=500)
    strips = recent_outcomes(conn, [r["test_id"] for r in rows])
    for r in rows:
        r["recent"] = strips.get(r["test_id"], [])
    return {"window_days": days, "tests": rows}


def test_detail(conn: sqlite3.Connection, test_id: str, days: int = 30) -> dict[str, Any]:
    tid = analysis.resolve_test_id(conn, test_id)
    stats = analysis.flake_stats(conn, days, [tid]).get(tid)
    q = conn.execute("SELECT reason, added_at, added_by FROM quarantine WHERE test_id = ?", (tid,)).fetchone()

    commits = [dict(r) for r in conn.execute(
        """SELECT ru.commit_sha AS sha, MIN(ru.started_at) AS first_at,
                  SUM(r.outcome = 'pass') AS passes, SUM(r.outcome IN ('fail','error')) AS fails
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? AND r.outcome != 'skip'
           GROUP BY ru.commit_sha ORDER BY first_at DESC LIMIT ?""", (tid, FIELD_COMMITS))][::-1]

    signatures = [dict(r) for r in conn.execute(
        """SELECT r.failure_sig AS sig, COUNT(*) AS count, MAX(ru.started_at) AS last_at,
                  MAX(r.message) AS sample
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? AND r.failure_sig IS NOT NULL AND ru.started_at >= ?
           GROUP BY r.failure_sig ORDER BY count DESC LIMIT 10""", (tid, since(days)))]

    executions = [dict(r) for r in conn.execute(
        """SELECT ru.run_id, ru.started_at, ru.commit_sha AS sha, ru.attempt, r.retry, r.outcome,
                  r.duration_ms, r.failure_sig, r.flags
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? ORDER BY ru.started_at DESC, ru.run_id DESC, r.retry DESC LIMIT 60""", (tid,))]

    day_list = day_range(days)
    per_day: dict[str, list[int]] = defaultdict(list)
    for day, ms in conn.execute(
        """SELECT substr(ru.started_at, 1, 10), r.duration_ms FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? AND r.outcome = 'pass' AND r.duration_ms IS NOT NULL AND ru.started_at >= ?""",
        (tid, since(days))):
        per_day[day].append(ms)

    issues = [dict(r) for r in conn.execute(
        "SELECT number, title, state, url, managed_key FROM issues WHERE managed_key IN (?, ?) ORDER BY state = 'open' DESC, number DESC",
        (f"flaky:{tid}", f"perf:{tid}"))]
    from .delivery import test_metrics
    return {
        "test_id": tid,
        "window_days": days,
        "stats": stats,
        "quarantine": dict(q) if q else None,
        "issues": issues,
        "metrics": test_metrics(conn, tid),
        "commits": commits,
        "signatures": signatures,
        "executions": executions,
        "durations": {"days": day_list,
                      "values": [float(statistics.median(per_day[d])) if d in per_day else None for d in day_list]},
    }


def runs_list(conn: sqlite3.Connection, limit: int = 50, window_days: int = 30) -> dict[str, Any]:
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,))]
    for r in runs:
        t = analysis.triage_run(conn, run_id=r["run_id"], window_days=window_days)
        r["tests"] = t["run"]["tests"]
        r["decision"] = t["decision"]
        r["failing"] = len(t["failures"])
        r["blocking"] = len(t["blocking"])
        r["summary"] = t["summary"]
    from . import commits
    commits.for_runs(conn, runs)
    return {"runs": runs}


def quarantine_view(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM quarantine ORDER BY added_at DESC")]
    stats = analysis.flake_stats(conn, days, [r["test_id"] for r in rows]) if rows else {}
    strips = recent_outcomes(conn, [r["test_id"] for r in rows])
    for r in rows:
        s = stats.get(r["test_id"], {})
        r["flip_shas"] = s.get("flip_shas", 0)
        r["eligible_shas"] = s.get("eligible_shas", 0)
        r["recent"] = strips.get(r["test_id"], [])
    sweep = analysis.sweep(conn, days, apply=False)
    cand = [c["test_id"] for c in sweep["to_quarantine"]]
    cand_strips = recent_outcomes(conn, cand)
    for c in sweep["to_quarantine"]:
        c["recent"] = cand_strips.get(c["test_id"], [])
    return {"quarantined": rows, "suggested": sweep["to_quarantine"], "release_candidates": sweep["release_candidates"]}
