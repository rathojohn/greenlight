"""Claude Code token usage: how many tokens went into each pull request, and into each test while it failed.

Where the numbers come from: every Claude Code session keeps a transcript (JSONL) with the usage of each API
response: model, input, output, cache reads and writes, a timestamp and the git branch the session was on.
`greenlight usage record` runs as a Stop hook (after every turn), reads the transcript and its subagents', and
stores per-minute totals. Only counts, model names, branch names and timestamps leave the machine, never
prompts or code. The transcript is Claude Code's own file, not a documented interface, so a change to its
format would need a change here.

Attribution:
- a pull request gets the tokens spent on its head branch until it merged or closed (a branch name reused by
  a later PR counts toward that one; work on a branch after its PR closed counts toward none);
- a test gets the tokens a session spent while the test was red: from a run in that session that failed it to
  the next run in the same session that passed it, or the session's last activity. Two tests red at once both
  count the same tokens, so these don't add up across tests.
Standard library only, like the rest of the CLI.
"""
from __future__ import annotations

import json
import os
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import Any

from .db import day_range, iso, norm_time, since, utcnow

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MAX_ROWS = 50_000  # one session's minutes; a payload bigger than this isn't usage


def agent_session(env: dict[str, str] | None = None) -> dict[str, str | None]:
    """The Claude Code session this process runs in, if any. A cloud session has two ids: Claude Code's own
    (CLAUDE_CODE_SESSION_ID, the transcript's) and the claude.ai one (session_..., the one playtest records
    carry). Runs record the claude.ai one when there is one, so a person can open it."""
    env = os.environ if env is None else env
    local = env.get("CLAUDE_CODE_SESSION_ID") or None
    remote = (env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or "").replace("cse_", "session_", 1) or None
    return {"session_id": local, "remote_session": remote,
            "url": f"https://claude.ai/code/{remote}" if remote else None}


def read_transcript(path: str | Path) -> list[dict[str, Any]]:
    """One entry per API response in a session's transcript and its subagents' transcripts. A response written
    across several lines (one per content block) repeats the same usage, so it's counted once."""
    path = Path(path)
    files = [path, *sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))]
    seen: set[str] = set()
    out = []
    for f in files:
        try:
            lines = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                if '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) else None
                if e.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                key = m.get("id") or e.get("requestId") or e.get("uuid")
                model = m.get("model") or "unknown"
                if not key or key in seen or model == "<synthetic>":
                    continue
                seen.add(key)
                u = m["usage"]
                tokens = [int(u.get(k) or 0) for k in _USAGE_KEYS]
                ts = norm_time(e.get("timestamp"))
                if ts and any(tokens):
                    out.append({"at": ts, "model": model, "branch": e.get("gitBranch") or "", "session": e.get("sessionId"),
                                **dict(zip(FIELDS, tokens))})
    return out


def minute_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (minute, model, branch) totals: what gets stored and sent."""
    rows: dict[tuple, dict[str, Any]] = {}
    for e in entries:
        key = (e["at"][:16] + ":00+00:00", e["model"], e["branch"])
        r = rows.setdefault(key, {"minute": key[0], "model": key[1], "branch": key[2], "requests": 0,
                                  **{f: 0 for f in FIELDS}})
        r["requests"] += 1
        for f in FIELDS:
            r[f] += e[f]
    return sorted(rows.values(), key=lambda r: (r["minute"], r["model"], r["branch"]))


def payload_from_hook(hook: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """What a Stop hook sends: the session and all its minutes so far (resending replaces them)."""
    transcript = hook.get("transcript_path")
    if not transcript:
        return None
    entries = read_transcript(transcript)
    me = agent_session(env)
    session_id = hook.get("session_id") or me["session_id"] or Path(transcript).stem
    remote = me["remote_session"] if me["session_id"] in (None, session_id) else None  # only for this session
    repo = None
    if hook.get("cwd"):
        from .config import remote_repo
        repo = remote_repo(hook["cwd"])
    return {"session": {"session_id": session_id, "remote_session": remote, "repo": repo,
                        "agent": "claude-code"},
            "rows": minute_rows(entries)}


def store(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Replace one session's usage with what the payload says (a transcript only grows, so the latest
    report is the whole story)."""
    s = payload.get("session") if isinstance(payload, dict) else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(s, dict) or not s.get("session_id") or not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("send {\"session\": {\"session_id\": ...}, \"rows\": [...]}")
    clean = []
    for r in rows:
        minute = norm_time(str(r.get("minute") or "")) if isinstance(r, dict) else None
        if not minute:
            raise ValueError("every row needs a minute (ISO 8601)")
        clean.append((s["session_id"], minute, str(r.get("model") or "unknown"), str(r.get("branch") or ""),
                      int(r.get("requests") or 0), *(int(r.get(f) or 0) for f in FIELDS)))
    first = min((c[1] for c in clean), default=None)
    last = max((c[1] for c in clean), default=None)
    with conn:
        conn.execute("DELETE FROM agent_usage WHERE session_id = ?", (s["session_id"],))
        conn.executemany("INSERT OR REPLACE INTO agent_usage (session_id, minute, model, branch, requests, input_tokens, "
                         "output_tokens, cache_read_tokens, cache_write_tokens) VALUES (?,?,?,?,?,?,?,?,?)", clean)
        conn.execute(
            """INSERT INTO agent_sessions (session_id, remote_session, repo, agent, first_at, last_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET remote_session = COALESCE(excluded.remote_session, remote_session),
                 repo = COALESCE(excluded.repo, repo), first_at = excluded.first_at, last_at = excluded.last_at,
                 updated_at = excluded.updated_at""",
            (s["session_id"], s.get("remote_session"), s.get("repo"), s.get("agent") or "claude-code", first, last,
             iso(utcnow())))
    return {"session_id": s["session_id"], "minutes": len(clean)}


def _sum(rows: list) -> dict[str, int]:
    out = {"requests": 0, **{f: 0 for f in FIELDS}}
    for r in rows:
        out["requests"] += r["requests"]
        for f in FIELDS:
            out[f] += r[f]
    return out


def _by_pr(conn: sqlite3.Connection, rows: list) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prs = [dict(r) for r in conn.execute(
        "SELECT number, title, state, head, created_at, merged_at, closed_at, url FROM pull_requests "
        "WHERE head IS NOT NULL ORDER BY created_at")]
    by_head: dict[str, list[dict]] = defaultdict(list)
    for p in prs:
        p["end"] = p["merged_at"] or p["closed_at"] or "9999"
        by_head[p["head"]].append(p)
    for heads in by_head.values():
        heads.sort(key=lambda p: p["end"])
    got: dict[int, list] = defaultdict(list)
    sessions: dict[int, set] = defaultdict(set)
    loose = []
    for r in rows:
        pr = next((p for p in by_head.get(r["branch"], []) if r["minute"] <= p["end"]), None)
        if pr:
            got[pr["number"]].append(r)
            sessions[pr["number"]].add(r["session_id"])
        else:
            loose.append(r)
    out = []
    for p in prs:
        if p["number"] in got:
            out.append({k: p[k] for k in ("number", "title", "state", "head", "created_at", "merged_at", "url")}
                       | {"sessions": len(sessions[p["number"]])} | _sum(got[p["number"]]))
    out.sort(key=lambda p: -p["output_tokens"])
    return out, _sum(loose)


def _by_test(conn: sqlite3.Connection, rows: list, start: str) -> list[dict[str, Any]]:
    """Tokens a session spent while each test was red (see the module docstring)."""
    ids: dict[str, str] = {}
    for sid, remote in conn.execute("SELECT session_id, remote_session FROM agent_sessions"):
        ids[sid] = sid
        if remote:
            ids[remote] = sid
    if not ids:
        return []
    usage: dict[str, tuple[list, list]] = {}
    for r in sorted(rows, key=lambda r: r["minute"]):
        minutes, totals = usage.setdefault(r["session_id"], ([], []))
        minutes.append(r["minute"])
        totals.append(r)
    marks = ",".join("?" * len(ids))
    runs = conn.execute(f"SELECT run_id, session, started_at FROM runs WHERE session IN ({marks}) AND started_at >= ? "
                        "ORDER BY started_at", (*ids, start)).fetchall()
    finals: dict[int, dict[str, str]] = defaultdict(dict)  # run -> test -> outcome of its last retry
    if runs:
        rmarks = ",".join("?" * len(runs))
        for run_id, test_id, outcome in conn.execute(
                f"SELECT run_id, test_id, outcome FROM results WHERE run_id IN ({rmarks}) ORDER BY retry",
                [r[0] for r in runs]):
            finals[run_id][test_id] = outcome
    per_session: dict[str, list] = defaultdict(list)
    for run_id, session, started in runs:
        per_session[ids[session]].append((started, finals.get(run_id, {})))
    out: dict[str, dict[str, Any]] = {}

    def charge(sid: str, test: str, a: str, b: str | None, still: bool) -> None:
        minutes, totals = usage.get(sid, ([], []))
        lo = bisect_left(minutes, a[:16] + ":00+00:00")
        hi = bisect_left(minutes, b[:16] + ":00+00:00") if b else len(minutes)
        t = out.setdefault(test, {"test_id": test, "times_red": 0, "still_red": 0, "sessions": set(),
                                  "requests": 0, **{f: 0 for f in FIELDS}})
        t["times_red"] += 1
        t["still_red"] += still
        t["sessions"].add(sid)
        for r in totals[lo:hi]:
            t["requests"] += r["requests"]
            for f in FIELDS:
                t[f] += r[f]

    for sid, session_runs in per_session.items():
        red: dict[str, str] = {}
        for started, outcomes in session_runs:
            for test, outcome in outcomes.items():
                if outcome in ("fail", "error") and test not in red:
                    red[test] = started
                elif outcome == "pass" and test in red:
                    charge(sid, test, red.pop(test), started, False)
        for test, began in red.items():
            charge(sid, test, began, None, True)
    tests = [t | {"sessions": len(t["sessions"])} for t in out.values()]
    return sorted(tests, key=lambda t: -t["output_tokens"])


def summary(conn: sqlite3.Connection, days: int = 30, pr: int | None = None,
            test_id: str | None = None) -> dict[str, Any]:
    start = since(days)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM agent_usage WHERE minute >= ? ORDER BY minute", (start,))]
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {f: 0 for f in FIELDS})
    models: dict[str, list] = defaultdict(list)
    per_session: dict[str, list] = defaultdict(list)
    for r in rows:
        for f in FIELDS:
            daily[r["minute"][:10]][f] += r[f]
        models[r["model"]].append(r)
        per_session[r["session_id"]].append(r)
    days_ = day_range(days)
    by_pr, unattributed = _by_pr(conn, rows)
    by_test = _by_test(conn, rows, start)
    meta = {r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM agent_sessions")}
    sessions = []
    for sid, rs in per_session.items():
        m = meta.get(sid, {})
        remote = m.get("remote_session")
        sessions.append({"session_id": sid, "remote_session": remote, "repo": m.get("repo"),
                         "url": f"https://claude.ai/code/{remote}" if remote else None,
                         "first_at": rs[0]["minute"], "last_at": rs[-1]["minute"],
                         "branches": sorted({r["branch"] for r in rs if r["branch"]}),
                         "models": sorted({r["model"] for r in rs})} | _sum(rs))
    sessions.sort(key=lambda s: s["last_at"], reverse=True)
    out = {
        "window_days": days, "days": days_,
        "output_per_day": [daily[d]["output_tokens"] for d in days_],
        "input_per_day": [daily[d]["input_tokens"] + daily[d]["cache_write_tokens"] for d in days_],
        "cache_read_per_day": [daily[d]["cache_read_tokens"] for d in days_],
        "totals": _sum(rows) | {"sessions": len(per_session)},
        "models": sorted(({"model": k} | _sum(v) for k, v in models.items()), key=lambda m: -m["output_tokens"]),
        "by_pr": by_pr, "unattributed": unattributed, "by_test": by_test, "sessions": sessions[:50],
    }
    if pr is not None:
        out = {"window_days": days, "pr": next((p for p in by_pr if p["number"] == pr), None)}
    elif test_id is not None:
        out = {"window_days": days, "test": next((t for t in by_test if t["test_id"] == test_id), None)}
    return out


def window_tokens(conn: sqlite3.Connection, test_id: str, days: int) -> dict[str, Any] | None:
    rows = [dict(r) for r in conn.execute("SELECT * FROM agent_usage WHERE minute >= ? ORDER BY minute", (since(days),))]
    return next((t for t in _by_test(conn, rows, since(days)) if t["test_id"] == test_id), None)
