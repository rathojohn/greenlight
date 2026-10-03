"""Flake scoring, run triage (the gate), and quarantine management.

Core definition: a test is nondeterministic on a commit if it both passed and failed on that
same SHA (across reruns or in-run retries). We call that SHA a "flip". Only SHAs where the test
executed at least twice could have flipped, so those are the denominator ("eligible" SHAs).
"""
from __future__ import annotations

import math
import time
import sqlite3
from collections import defaultdict
from typing import Any

from .db import iso, since, utcnow

FAIL = ("fail", "error")
DEFAULT_WINDOW_DAYS = 30
DEFAULT_MIN_FLIPS = 2       # flips on 2+ distinct SHAs = flaky; 1 = suspect
DEFAULT_MAX_ATTEMPTS = 3    # a "flaky" test that fails this many times on one SHA with no pass is treated as real

PASS, RERUN, REAL = "PASS", "RERUN_TARGETED", "REAL_FAILURE"
EXIT_CODES = {PASS: 0, REAL: 1, RERUN: 2}
BLOCKING = {"new_test", "real_failure"}
RERUNNABLE = {"known_flaky", "suspect_flaky"}


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson interval. Ranks 5/10 above 1/1, which a raw rate would not."""
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def classify(flips: int, fails: int, min_flips: int = DEFAULT_MIN_FLIPS) -> str:
    if flips >= min_flips:
        return "flaky"
    if flips >= 1:
        return "suspect"
    if fails > 0:
        return "failing"
    return "stable"


def flake_stats(
    conn: sqlite3.Connection,
    window_days: int = DEFAULT_WINDOW_DAYS,
    test_ids: list[str] | None = None,
    min_flips: int = DEFAULT_MIN_FLIPS,
) -> dict[str, dict[str, Any]]:
    """Per-test flip counts over the window, keyed by test_id."""
    params: list[Any] = [since(window_days)]
    test_filter = ""
    if test_ids is not None:
        if not test_ids:
            return {}
        test_filter = f"AND r.test_id IN ({','.join('?' * len(test_ids))})"
        params.extend(test_ids)

    sql = f"""
    WITH scoped AS (
        SELECT r.test_id, r.outcome, r.failure_sig, ru.commit_sha, ru.started_at
        FROM results r JOIN runs ru ON ru.run_id = r.run_id
        WHERE ru.started_at >= ? AND r.outcome != 'skip' {test_filter}
    ),
    per_sha AS (
        SELECT test_id, commit_sha,
               COUNT(*)                                   AS execs,
               SUM(outcome = 'pass')                      AS passes,
               SUM(outcome IN ('fail', 'error'))          AS fails,
               MAX(CASE WHEN outcome IN ('fail', 'error') THEN started_at END) AS last_fail_at
        FROM scoped GROUP BY test_id, commit_sha
    ),
    sigs AS (
        SELECT test_id, COUNT(DISTINCT failure_sig) AS distinct_sigs
        FROM scoped WHERE failure_sig IS NOT NULL GROUP BY test_id
    )
    SELECT p.test_id,
           COUNT(*)                                       AS shas,
           SUM(p.execs >= 2)                              AS eligible_shas,
           SUM(p.passes > 0 AND p.fails > 0)              AS flip_shas,
           SUM(p.execs)                                   AS executions,
           SUM(p.fails)                                   AS failures,
           MAX(CASE WHEN p.passes > 0 AND p.fails > 0 THEN p.last_fail_at END) AS last_flip_at,
           COALESCE(s.distinct_sigs, 0)                   AS distinct_failure_sigs,
           q.test_id IS NOT NULL                          AS quarantined
    FROM per_sha p
    LEFT JOIN sigs s ON s.test_id = p.test_id
    LEFT JOIN quarantine q ON q.test_id = p.test_id
    GROUP BY p.test_id
    """
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(sql, params):
        d = dict(row)
        flips, eligible = d["flip_shas"], d["eligible_shas"]
        d["quarantined"] = bool(d["quarantined"])
        d["flake_rate"] = round(flips / eligible, 3) if eligible else 0.0
        d["flake_score"] = round(wilson_lower(flips, eligible), 3)
        d["classification"] = classify(flips, d["failures"], min_flips)
        out[d["test_id"]] = d
    return out


def list_flaky(
    conn: sqlite3.Connection,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_flips: int = 1,
    include_quarantined: bool = True,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Tests with at least min_flips flips, ranked by Wilson score then flip count."""
    stats = flake_stats(conn, window_days).values()
    rows = [s for s in stats if s["flip_shas"] >= min_flips and (include_quarantined or not s["quarantined"])]
    rows.sort(key=lambda s: (s["flake_score"], s["flip_shas"]), reverse=True)
    return rows[:limit]


def resolve_test_id(conn: sqlite3.Connection, test_id: str) -> str:
    if conn.execute("SELECT 1 FROM results WHERE test_id = ? LIMIT 1", (test_id,)).fetchone():
        return test_id
    matches = [r[0] for r in conn.execute(
        "SELECT DISTINCT test_id FROM results WHERE test_id LIKE ? LIMIT 6", (f"%{test_id}%",))]
    if len(matches) == 1:
        return matches[0]
    hint = f" Closest matches: {matches[:5]}" if matches else " Check the id, or list candidates with `greenlight flaky`."
    raise LookupError(f"No test with id '{test_id}'.{hint}")


def test_history(conn: sqlite3.Connection, test_id: str, limit: int = 30) -> dict[str, Any]:
    test_id = resolve_test_id(conn, test_id)
    rows = conn.execute(
        """SELECT ru.run_id, ru.started_at, substr(ru.commit_sha, 1, 10) AS sha, ru.branch, ru.attempt,
                  r.retry, r.outcome, r.duration_ms, r.failure_sig, substr(r.message, 1, 160) AS message
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? ORDER BY ru.started_at DESC, ru.run_id DESC, r.retry DESC LIMIT ?""",
        (test_id, limit),
    ).fetchall()
    stats = flake_stats(conn, DEFAULT_WINDOW_DAYS, [test_id]).get(test_id)
    q = conn.execute("SELECT reason, added_at, added_by FROM quarantine WHERE test_id = ?", (test_id,)).fetchone()
    return {"test_id": test_id, "stats_30d": stats, "quarantine": dict(q) if q else None,
            "history": [dict(r) for r in rows]}


def resolve_run(conn: sqlite3.Connection, run_id: int | None = None, commit_sha: str | None = None) -> sqlite3.Row:
    if run_id is not None:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    elif commit_sha:
        row = conn.execute(
            "SELECT * FROM runs WHERE commit_sha LIKE ? ORDER BY started_at DESC, run_id DESC LIMIT 1",
            (f"{commit_sha}%",)).fetchone()
    else:
        row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
    if row is None:
        what = f"run_id={run_id}" if run_id is not None else f"sha={commit_sha}" if commit_sha else "any run"
        raise LookupError(f"No run found for {what}. Ingest results first with `greenlight ingest`.")
    return row


def triage_run(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    commit_sha: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_flips: int = DEFAULT_MIN_FLIPS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Decide what a run's failures mean: PASS, RERUN_TARGETED (only flaky failures), or REAL_FAILURE."""
    run = resolve_run(conn, run_id, commit_sha)
    rid, sha = run["run_id"], run["commit_sha"]

    attempts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM results WHERE run_id = ? ORDER BY test_id, retry", (rid,)):
        attempts[r["test_id"]].append(r)

    failing = {t: a[-1] for t, a in attempts.items() if a[-1]["outcome"] in FAIL}
    passed_on_retry = sorted(t for t, a in attempts.items()
                             if a[-1]["outcome"] == "pass" and any(x["outcome"] in FAIL for x in a[:-1]))

    ids = list(failing)
    marks = ",".join("?" * len(ids))
    stats = flake_stats(conn, window_days, ids, min_flips)
    quarantined = {r[0] for r in conn.execute(f"SELECT test_id FROM quarantine WHERE test_id IN ({marks})", ids)} if ids else set()
    new_tests = {r[0] for r in conn.execute(
        f"SELECT test_id FROM results WHERE test_id IN ({marks}) GROUP BY test_id HAVING MIN(run_id) = ?",
        [*ids, rid])} if ids else set()
    on_sha = {r["test_id"]: (r["passes"], r["fails"]) for r in conn.execute(
        f"""SELECT r.test_id, SUM(r.outcome = 'pass') AS passes, SUM(r.outcome IN ('fail','error')) AS fails
            FROM results r JOIN runs ru ON ru.run_id = r.run_id
            WHERE ru.commit_sha = ? AND r.test_id IN ({marks}) GROUP BY r.test_id""", [sha, *ids])} if ids else {}
    prior_sigs: dict[str, set[str]] = defaultdict(set)
    if ids:
        for r in conn.execute(
            f"""SELECT r.test_id, r.failure_sig FROM results r JOIN runs ru ON ru.run_id = r.run_id
                WHERE r.test_id IN ({marks}) AND r.run_id != ? AND r.failure_sig IS NOT NULL
                  AND ru.started_at >= ?""", [*ids, rid, since(window_days)]):
            prior_sigs[r[0]].add(r[1])

    failures = []
    for t, final in sorted(failing.items()):
        s = stats.get(t, {})
        cls = s.get("classification", "stable")
        passes_sha, fails_sha = on_sha.get(t, (0, 0))
        if t in quarantined:
            category = "quarantined"
        elif t in new_tests and run["total_tests"] is None:  # a partial run (playtest record) can't tell new from never-failed
            category = "new_test"
        elif cls in ("flaky", "suspect"):
            category = "known_flaky" if cls == "flaky" else "suspect_flaky"
            if passes_sha == 0 and fails_sha >= max_attempts:
                category = "real_failure"  # flaky history, but it has never passed on this commit
        else:
            category = "real_failure"
        sig = final["failure_sig"]
        failures.append({
            "test_id": t,
            "category": category,
            "flip_shas": s.get("flip_shas", 0),
            "eligible_shas": s.get("eligible_shas", 0),
            "flake_rate": s.get("flake_rate", 0.0),
            "this_sha": {"passes": passes_sha, "fails": fails_sha},
            "failure_sig": sig,
            "new_signature": bool(sig and prior_sigs[t] and sig not in prior_sigs[t]),
            "message": (final["message"] or "")[:200] or None,
        })

    blocking = [f["test_id"] for f in failures if f["category"] in BLOCKING]
    rerun = [f["test_id"] for f in failures if f["category"] in RERUNNABLE]
    decision = REAL if blocking else RERUN if rerun else PASS

    counts: dict[str, int] = defaultdict(int)
    for f in failures:
        counts[f["category"]] += 1
    summary = (f"{len(failures)} failing test(s): " + ", ".join(f"{n} {c}" for c, n in sorted(counts.items()))
               if failures else "No failures.")
    if passed_on_retry:
        summary += f" {len(passed_on_retry)} passed only on retry."

    return {
        "run": {"run_id": rid, "commit_sha": sha, "branch": run["branch"], "attempt": run["attempt"],
                "started_at": run["started_at"], "tests": run["total_tests"] or len(attempts),
                "results_named": len(attempts), "source": run["source"], "git_commit": run["git_commit"],
                "session": run["session"], "command": run["command"], "url": run["url"]},
        "decision": decision,
        "exit_code": EXIT_CODES[decision],
        "summary": summary,
        "rerun_tests": rerun,
        "blocking": blocking,
        "failures": failures,
        "passed_on_retry": passed_on_retry,
    }


def quarantine(conn: sqlite3.Connection, test_id: str, reason: str, added_by: str = "manual") -> str:
    test_id = resolve_test_id(conn, test_id)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO quarantine (test_id, reason, added_at, added_by) VALUES (?, ?, ?, ?)",
            (test_id, reason, iso(utcnow()), added_by))
    return test_id


def unquarantine(conn: sqlite3.Connection, test_id: str) -> bool:
    with conn:
        return conn.execute("DELETE FROM quarantine WHERE test_id = ?", (test_id,)).rowcount > 0


def forget_runs(conn: sqlite3.Connection, runs: list[Any], dry_run: bool = False) -> list[dict[str, Any]]:
    """Delete runs recorded by mistake, with their results and measurements. A run is named by its number
    (as the dashboard shows it) or its external id. Returns what was (or with dry_run, would be) deleted;
    names that match nothing are skipped, so asking twice is harmless."""
    if not isinstance(runs, list):
        raise ValueError("runs must be a list of run numbers or external ids")
    gone = []
    with conn:
        for ref in runs:
            col = "run_id" if isinstance(ref, int) or str(ref).isdigit() else "external_id"
            row = conn.execute(f"SELECT run_id, external_id, commit_sha, started_at FROM runs WHERE {col} = ?",
                               (int(ref) if col == "run_id" else str(ref),)).fetchone()
            if row:
                if not dry_run:
                    conn.execute("DELETE FROM runs WHERE run_id = ?", (row[0],))  # results and metrics cascade
                gone.append({"run_id": row[0], "external_id": row[1], "commit_sha": row[2], "started_at": row[3]})
    return gone


def sweep(
    conn: sqlite3.Connection,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_flips: int = DEFAULT_MIN_FLIPS,
    min_rate: float = 0.05,
    release_days: int = 14,
    release_min_runs: int = 10,
    apply: bool = False,
) -> dict[str, Any]:
    """Find flaky tests to quarantine and quarantined tests that look healthy again.
    Only quarantines when apply=True. Never auto-releases; that stays a human call."""
    stats = flake_stats(conn, window_days, min_flips=min_flips)
    to_quarantine = sorted(
        (s for s in stats.values()
         if s["classification"] == "flaky" and not s["quarantined"] and s["flake_rate"] >= min_rate),
        key=lambda s: s["flake_score"], reverse=True)

    recent = flake_stats(conn, release_days)
    release = [
        {"test_id": t, "executions": s["executions"], "failures": s["failures"]}
        for t, s in recent.items()
        if s["quarantined"] and s["executions"] >= release_min_runs and s["failures"] == 0
    ]

    if apply:
        for s in to_quarantine:
            quarantine(conn, s["test_id"],
                       f"auto: flipped on {s['flip_shas']}/{s['eligible_shas']} SHAs in {window_days}d",
                       added_by="auto")
    return {
        "applied": apply,
        "to_quarantine": [{k: s[k] for k in ("test_id", "flip_shas", "eligible_shas", "flake_rate", "flake_score")}
                          for s in to_quarantine],
        "release_candidates": release,
    }


QUERY_SECONDS = 5.0  # a query that runs longer is stopped, so one can't tie up a shared server
# What a query may do, whatever the connection allows: read tables and call functions (WITH ... DELETE is a SELECT
# to the prefix check, and a dashboard is saved on a connection that can write)
_READS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _only_reads(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READS else sqlite3.SQLITE_DENY


def _tokens(*values: Any) -> dict[str, Any]:
    return dict(zip(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens"),
                    values))


def _cost(input_tokens: Any, output_tokens: Any, cache_read: Any, cache_write: Any, cache_write_1h: Any = 0,
          model: Any = None) -> float:
    """cost(...) in SQL: tokens priced as input tokens of their model, as usage.weighted() prices them."""
    from .usage import _weigh
    return _weigh(_tokens(input_tokens, output_tokens, cache_read, cache_write, cache_write_1h), model)


def _usd(input_tokens: Any, output_tokens: Any, cache_read: Any, cache_write: Any, cache_write_1h: Any,
         model: Any) -> float | None:
    """usd(...) in SQL: the same tokens in dollars at the model's API list price, NULL for a model without one."""
    from .usage import dollars
    return dollars(_tokens(input_tokens, output_tokens, cache_read, cache_write, cache_write_1h), model)


def run_query(conn: sqlite3.Connection, sql: str, limit: int = 200, params: dict[str, Any] | None = None,
              seconds: float = QUERY_SECONDS, functions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ad-hoc read-only SQL. Pass a read-only connection; the statement check is a second guard. Named parameters
    (:start, :days) come from params; cost(input, output, cache_read, cache_write, cache_write_1h[, model]) prices
    tokens as input tokens and usd(the same, model) in dollars."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped.lower().startswith(("select", "with")) or ";" in stripped:
        raise ValueError("Only a single SELECT (or WITH ... SELECT) statement is allowed.")
    conn.create_function("cost", -1, _cost, deterministic=True)
    conn.create_function("usd", 6, _usd, deterministic=True)
    for name, fn in (functions or {}).items():
        conn.create_function(name, 1, fn, deterministic=True)
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    conn.set_authorizer(_only_reads)
    try:
        cur = conn.execute(stripped, params or {})
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(limit + 1)
    except sqlite3.DatabaseError as e:
        if time.monotonic() > deadline:
            raise ValueError(f"The query ran longer than {seconds:g} seconds and was stopped.") from e
        if "not authorized" in str(e):
            raise ValueError("Only reads are allowed: SELECT from tables and call functions.") from e
        raise ValueError(f"SQL: {e}") from e
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    return {"columns": cols, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit}
