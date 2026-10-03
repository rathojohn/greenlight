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

from . import context
from .db import day_range, iso, norm_time, since, utcnow

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens")
# What each kind of token costs next to an input token, from Claude's API prices: output 5x, a cache read 0.1x,
# a cache write 1.25x, or 2x when it's written for an hour (cache_write_1h_tokens is the part that was)
WEIGHTS = {"input_tokens": 1, "output_tokens": 5, "cache_read_tokens": 0.1, "cache_write_tokens": 1.25,
           "cache_write_1h_tokens": 0.75}
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


def transcript_files(path: str | Path) -> list[tuple[Path, str]]:
    """A session's transcript and its subagents', each with the subagent's name ('' for the session)."""
    path = Path(path)
    return [(path, ""), *((p, p.stem) for p in sorted((path.parent / path.stem / "subagents").glob("*.jsonl")))]


def read_files(path: str | Path, test: str | None = None, root: str | None = None) -> list[dict[str, Any]]:
    return [context.read_file(p, agent, test, root) | {"agent": agent} for p, agent in transcript_files(path)]


def entries(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per API response (read_file counts a response written across several lines once)."""
    return [{"at": r["at"], "model": r["model"], "branch": r["branch"], "session": r["session"],
             **{k: r[k] for k in FIELDS}} for f in files for r in f["requests"] if any(r[k] for k in FIELDS)]


def read_transcript(path: str | Path) -> list[dict[str, Any]]:
    """One entry per API response in a session's transcript and its subagents' transcripts."""
    return entries(read_files(path))


def weighted(row: dict[str, Any]) -> int:
    """Tokens priced as input tokens: what the row cost, in the unit every model shares."""
    return round(sum(row.get(k, 0) * w for k, w in WEIGHTS.items()))


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
    repo = test = None
    labels = True
    root = hook.get("cwd")
    if root:
        from . import config
        repo = config.remote_repo(root)
        try:
            cfg = config.load(start=Path(root))
            test, labels = cfg.get("usage", "test_commands"), cfg.get("usage", "item_labels") is not False
        except (ValueError, OSError):
            pass
    files = read_files(transcript, test, root)
    me = agent_session(env)
    session_id = hook.get("session_id") or me["session_id"] or Path(transcript).stem
    remote = me["remote_session"] if me["session_id"] in (None, session_id) else None  # only for this session
    return {"session": {"session_id": session_id, "remote_session": remote, "repo": repo,
                        "agent": "claude-code"},
            "rows": minute_rows(entries(files)), "context": context.rows(files),
            "rebuilds": [r | {"agent": f["agent"]} for f in files for r in f["rebuilds"]],
            "switches": [r | {"agent": f["agent"]} for f in files for r in f["switches"]],
            **({"items": context.item_rows(files, context.instruction_files(root)), "adds": context.add_rows(files)}
               if labels else {})}


CONTEXT_FIELDS = ("calls", "tokens", "carried_tokens", "repeat_reads", "repeat_tokens")


def _minute(r: Any) -> str:
    minute = norm_time(str(r.get("minute") or r.get("at") or "")) if isinstance(r, dict) else None
    if not minute:
        raise ValueError("every row needs a minute (ISO 8601)")
    return minute


def store(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Replace one session's usage with what the payload says (a transcript only grows, so the latest
    report is the whole story). A payload without context or rebuilds (an older client) leaves those alone."""
    s = payload.get("session") if isinstance(payload, dict) else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    extra = ([payload.get(k) for k in ("context", "rebuilds", "switches", "items", "adds")] if isinstance(payload, dict)
             else [None] * 5)
    if (not isinstance(s, dict) or not s.get("session_id") or not isinstance(rows, list) or len(rows) > MAX_ROWS
            or any(not isinstance(x, (list, type(None))) or len(x or ()) > MAX_ROWS for x in extra)):
        raise ValueError("send {\"session\": {\"session_id\": ...}, \"rows\": [...]}")
    ctx, rebuilds, switches, items, adds = extra
    sid = s["session_id"]
    clean = [(sid, _minute(r), str(r.get("model") or "unknown"), str(r.get("branch") or ""),
              int(r.get("requests") or 0), *(int(r.get(f) or 0) for f in FIELDS)) for r in rows]
    ctx_rows = [(sid, _minute(r), str(r.get("branch") or ""), str(r.get("category") or "other")[:64],
                 *(int(r.get(f) or 0) for f in CONTEXT_FIELDS)) for r in ctx or ()]
    rebuild_rows = [(sid, str(r.get("agent") or "")[:200], _minute(r), str(r.get("branch") or ""),
                     int(r.get("tokens") or 0), r.get("ttl") if r.get("ttl") in ("1h", "5m") else None,
                     int(r["idle_seconds"]) if isinstance(r.get("idle_seconds"), (int, float)) else None,
                     str(r.get("cause") or "other")[:32]) for r in rebuilds or ()]
    switch_rows = [(sid, str(r.get("agent") or "")[:200], _minute(r), str(r.get("from_branch") or ""),
                    str(r.get("to_branch") or ""), int(r.get("context_tokens") or 0), int(r.get("carried_tokens") or 0))
                   for r in switches or ()]
    item_rows = [(sid, str(r.get("label") or "")[:300], str(r.get("kind") or "tool")[:32], str(r.get("group") or "")[:300],
                  norm_time(str(r.get("seg_start") or r.get("first_at") or "")) or "", str(r.get("branch") or ""),
                  *(int(r.get(f) or 0) for f in context.ITEM_FIELDS)) for r in items or () if r.get("label")]
    add_rows = [(sid, str(r.get("label") or "")[:300], str(r.get("kind") or "tool")[:32], _minute(r) if r.get("at") else "",
                 str(r.get("branch") or ""), int(r.get("tokens") or 0), int(r.get("rides") or 0),
                 int(r.get("carried_tokens") or 0)) for r in adds or () if r.get("label") and r.get("at")]
    first = min((c[1] for c in clean), default=None)
    last = max((c[1] for c in clean), default=None)
    with conn:
        conn.execute("DELETE FROM agent_usage WHERE session_id = ?", (sid,))
        conn.executemany(f"INSERT OR REPLACE INTO agent_usage (session_id, minute, model, branch, requests, "
                         f"{', '.join(FIELDS)}) VALUES (?,?,?,?,?{',?' * len(FIELDS)})", clean)
        if ctx is not None:
            conn.execute("DELETE FROM agent_context WHERE session_id = ?", (sid,))
            conn.executemany(f"INSERT OR REPLACE INTO agent_context (session_id, minute, branch, category, "
                             f"{', '.join(CONTEXT_FIELDS)}) VALUES (?,?,?,?{',?' * len(CONTEXT_FIELDS)})", ctx_rows)
        if rebuilds is not None:
            conn.execute("DELETE FROM agent_cache_rebuilds WHERE session_id = ?", (sid,))
            conn.executemany("INSERT OR REPLACE INTO agent_cache_rebuilds (session_id, agent, at, branch, tokens, ttl, "
                             "idle_seconds, cause) VALUES (?,?,?,?,?,?,?,?)", rebuild_rows)
        if items is not None:
            conn.execute("DELETE FROM agent_context_items WHERE session_id = ?", (sid,))
            conn.executemany(f"INSERT OR REPLACE INTO agent_context_items (session_id, label, kind, grp, seg_start, branch, "
                             f"{', '.join(context.ITEM_FIELDS)}) VALUES (?,?,?,?,?,?{',?' * len(context.ITEM_FIELDS)})",
                             item_rows)
        if adds is not None:
            conn.execute("DELETE FROM agent_context_adds WHERE session_id = ?", (sid,))
            conn.executemany("INSERT OR REPLACE INTO agent_context_adds (session_id, label, kind, at, branch, tokens, rides, "
                             "carried_tokens) VALUES (?,?,?,?,?,?,?,?)", add_rows)
        if switches is not None:
            conn.execute("DELETE FROM agent_task_switches WHERE session_id = ?", (sid,))
            conn.executemany("INSERT OR REPLACE INTO agent_task_switches (session_id, agent, at, from_branch, to_branch, "
                             "context_tokens, carried_tokens) VALUES (?,?,?,?,?,?,?)", switch_rows)
        conn.execute(
            """INSERT INTO agent_sessions (session_id, remote_session, repo, agent, first_at, last_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET remote_session = COALESCE(excluded.remote_session, remote_session),
                 repo = COALESCE(excluded.repo, repo), first_at = excluded.first_at, last_at = excluded.last_at,
                 updated_at = excluded.updated_at""",
            (sid, s.get("remote_session"), s.get("repo"), s.get("agent") or "claude-code", first, last,
             iso(utcnow())))
    return {"session_id": sid, "minutes": len(clean)}


def _sum(rows: list) -> dict[str, int]:
    out = {"requests": 0, **{f: 0 for f in FIELDS}}
    for r in rows:
        out["requests"] += r["requests"]
        for f in FIELDS:
            out[f] += r[f]
    return out | {"weighted": weighted(out)}


def _write_weight(conn: sqlite3.Connection, where: str = "1", args: tuple | list = ()) -> float:
    """What a cache write cost on average here: 1.25x, up to 2x as more of them were written for an hour."""
    w = conn.execute(f"SELECT SUM(cache_write_tokens), SUM(cache_write_1h_tokens) FROM agent_usage WHERE {where}",
                     args).fetchone()
    return WEIGHTS["cache_write_tokens"] + WEIGHTS["cache_write_1h_tokens"] * ((w[1] or 0) / w[0] if w[0] else 1)


def _context_cost(r: dict[str, Any], write_weight: float) -> int:
    return round(r["tokens"] * write_weight + r["carried_tokens"] * WEIGHTS["cache_read_tokens"])


def _categories(rows: list[dict[str, Any]], write_weight: float) -> list[dict[str, Any]]:
    cats: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = cats.setdefault(r["category"], {"category": r["category"], "sessions": set(), **{f: 0 for f in CONTEXT_FIELDS}})
        c["sessions"].add(r["session_id"])
        for f in CONTEXT_FIELDS:
            c[f] += r[f]
    out = [c | {"sessions": len(c["sessions"]), "weighted": _context_cost(c, write_weight)} for c in cats.values()]
    total = sum(c["weighted"] for c in out) or 1
    return sorted((c | {"share": round(c["weighted"] / total, 3)} for c in out), key=lambda c: -c["weighted"])


def items_summary(conn: sqlite3.Connection, start: str, sessions: set[str] | None = None,
                  where_extra: str = "", args_extra: tuple = ()) -> dict[str, Any]:
    """The things that rode along in context longest, by label, and by group (a folder and extension, a program):
    times added, tokens, requests that re-read them, and their cost (added at the cache write price, re-read at
    the cache read price)."""
    where, args = "seg_start >= ?", [start]
    if sessions is not None:
        where += f" AND session_id IN ({','.join('?' * len(sessions))})"
        args += sorted(sessions)
    ww = _write_weight(conn, where.replace("seg_start", "minute"), args)  # before the filter on items' own columns
    if where_extra:
        where += f" AND {where_extra}"
        args += list(args_extra)
    sums = ", ".join(f"SUM({f}) AS {f}" for f in ("adds", "tokens", "rides", "carried_tokens"))

    def rows(by: str) -> list[dict[str, Any]]:
        out = []
        for r in conn.execute(f"SELECT {by}, kind, COUNT(DISTINCT session_id) AS sessions, {sums}, MAX(max_rides) AS "
                              f"max_rides, COUNT(DISTINCT label) AS labels FROM agent_context_items WHERE {where} "
                              f"GROUP BY {by}, kind", args):
            d = dict(r)
            d["weighted"] = round(d["tokens"] * ww + d["carried_tokens"] * WEIGHTS["cache_read_tokens"])
            d["avg_rides"] = round(d["rides"] / d["adds"], 1) if d["adds"] else 0
            out.append(d)
        return sorted(out, key=lambda d: -d["weighted"])
    return {"items": rows("label")[:50], "groups": [g for g in rows("grp") if g["labels"] > 1][:20]}


def _item_totals(rows: list[dict[str, Any]], ww: float) -> list[dict[str, Any]]:
    """Item rows summed per (label, kind), priced like items_summary."""
    out: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        t = out.setdefault((r["label"], r["kind"]), {"label": r["label"], "kind": r["kind"], "sessions": set(), "adds": 0,
                                                     "tokens": 0, "rides": 0, "carried_tokens": 0, "max_rides": 0})
        t["sessions"].add(r["session_id"])
        for f in ("adds", "tokens", "rides", "carried_tokens"):
            t[f] += r.get(f) or 0
        t["max_rides"] = max(t["max_rides"], r.get("max_rides") or r.get("rides") or 0)
    return sorted(({**t, "sessions": len(t["sessions"]),
                    "weighted": round(t["tokens"] * ww + t["carried_tokens"] * WEIGHTS["cache_read_tokens"]),
                    "avg_rides": round(t["rides"] / t["adds"], 1) if t["adds"] else 0} for t in out.values()),
                  key=lambda t: -t["weighted"])


def _meta(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out = {}
    for r in conn.execute("SELECT session_id, remote_session, repo FROM agent_sessions"):
        out[r[0]] = {"session_id": r[0], "remote_session": r[1], "repo": r[2],
                     "url": f"https://claude.ai/code/{r[1]}" if r[1] else None}
    return out


def detail(conn: sqlite3.Connection, days: int, kind: str, key: str) -> dict[str, Any]:
    """One thing on the Token usage page, for its side panel: a session, a pull request, a test, a context
    category, or one kind of context that could have been dropped (switches, idle, repeats)."""
    start = since(days)
    meta = _meta(conn)
    if kind == "session":
        sid = next((m["session_id"] for m in meta.values() if key in (m["session_id"], m["remote_session"])), None)
        if not sid:
            raise LookupError(f"No usage recorded for session {key}")
        rows = [dict(r) for r in conn.execute("SELECT * FROM agent_usage WHERE session_id = ? ORDER BY minute", (sid,))]
        first, last = (rows[0]["minute"], rows[-1]["minute"]) if rows else (None, None)
        return {"kind": kind, "session": meta[sid] | {"first_at": first, "last_at": last,
                                                      "branches": sorted({r["branch"] for r in rows if r["branch"]}),
                                                      "models": sorted({r["model"] for r in rows})} | _sum(rows),
                "context": context_summary(conn, "0", {sid}), "prs": _by_pr(conn, rows)[0],
                "items": items_summary(conn, "0", {sid})["items"][:20],
                "rebuilds": [dict(r) for r in conn.execute(
                    "SELECT agent, at, branch, tokens, ttl, idle_seconds, cause FROM agent_cache_rebuilds "
                    "WHERE session_id = ? ORDER BY at", (sid,))],
                "switches": [dict(r) for r in conn.execute(
                    "SELECT agent, at, from_branch, to_branch, context_tokens, carried_tokens FROM agent_task_switches "
                    "WHERE session_id = ? ORDER BY at", (sid,))]}
    if kind == "pr":
        pr = conn.execute("SELECT head FROM pull_requests WHERE number = ?", (int(key),)).fetchone()
        if not pr or not pr[0]:
            raise LookupError(f"No pull request #{key} with a branch")
        _, by_head = _prs(conn, pr[0])
        mine = lambda r: (_pr_for(by_head, r["branch"], r["minute"]) or {}).get("number") == int(key)  # noqa: E731
        rows = [r for r in map(dict, conn.execute("SELECT * FROM agent_usage WHERE branch = ? ORDER BY minute", pr))
                if mine(r)]
        ctx = [r for r in map(dict, conn.execute("SELECT * FROM agent_context WHERE branch = ?", pr)) if mine(r)]
        p = next(x for x in by_head[pr[0]] if x["number"] == int(key))
        item_rows = [r for r in map(dict, conn.execute("SELECT * FROM agent_context_items WHERE branch = ?", pr))
                     if (_pr_for(by_head, r["branch"], r["seg_start"]) or {}).get("number") == int(key)]
        per: dict[str, list] = defaultdict(list)
        for r in rows:
            per[r["session_id"]].append(r)
        sessions = sorted((meta.get(sid, {"session_id": sid}) | {"first_at": rs[0]["minute"], "last_at": rs[-1]["minute"]}
                           | _sum(rs) for sid, rs in per.items()), key=lambda x: -x["weighted"])
        return {"kind": kind, "pr": {k: p[k] for k in ("number", "title", "state", "head", "created_at", "merged_at", "url")}
                | {"sessions": len(per)} | _sum(rows),
                "context": {"categories": _categories(ctx, _write_weight(conn))}, "sessions": sessions,
                "items": _item_totals(item_rows, _write_weight(conn))[:30]}
    if kind == "test":
        return {"kind": kind, "test": summary(conn, days, test_id=key)["test"], "items": _test_items(conn, key, start)}
    if kind in ("item", "group"):
        item_kind, _, label = key.partition(":")
        col = "label" if kind == "item" else "grp"
        found = items_summary(conn, start, where_extra=f"{col} = ? AND kind = ?", args_extra=(label, item_kind))
        rows = [dict(r) | {k: v for k, v in meta.get(r["session_id"], {}).items() if k != "session_id"}
                for r in conn.execute(f"SELECT * FROM agent_context_items WHERE {col} = ? AND kind = ? AND seg_start >= ? "
                                      "ORDER BY carried_tokens DESC", (label, item_kind, start))]
        if not rows:
            raise LookupError(f"Nothing called {label} rode along in the last {days} days")
        head = (found["items"] if kind == "item" else found["groups"] or rows)[0]
        by_session: dict[tuple, dict[str, Any]] = {}
        for r in rows:  # one row per session (or item, for a group) however many branch runs it spans
            k = (r["session_id"], r["label"]) if kind == "group" else (r["session_id"],)
            t = by_session.setdefault(k, {**r, "adds": 0, "tokens": 0, "rides": 0, "carried_tokens": 0})
            for f in ("adds", "tokens", "rides", "carried_tokens"):
                t[f] += r[f]
        _, by_head = _prs(conn)
        prs: dict[int, dict[str, Any]] = {}
        for r in rows:
            p = _pr_for(by_head, r["branch"], r["seg_start"])
            if p:
                t = prs.setdefault(p["number"], {k: p[k] for k in ("number", "title", "state", "url")}
                                   | {"adds": 0, "rides": 0, "carried_tokens": 0})
                for f in ("adds", "rides", "carried_tokens"):
                    t[f] += r[f]
        return {"kind": kind, "item": head,
                "rows": sorted(by_session.values(), key=lambda r: -r["carried_tokens"])[:50],
                "prs": sorted(prs.values(), key=lambda p: -p["carried_tokens"])[:20]}
    ww = _write_weight(conn, "minute >= ?", (start,))
    if kind == "category":
        rows = [dict(r) for r in conn.execute("SELECT * FROM agent_context WHERE category = ? AND minute >= ?",
                                              (key, start))]
        per: dict[str, list] = defaultdict(list)
        for r in rows:
            per[r["session_id"]].append(r)
        _, by_head = _prs(conn)
        prs: dict[int, list] = defaultdict(list)
        for r in rows:
            p = _pr_for(by_head, r["branch"], r["minute"])
            if p:
                prs[p["number"]].append(r)
        titles = {p["number"]: p for heads in by_head.values() for p in heads}
        top = lambda groups, head: sorted(  # noqa: E731
            (head(k) | _categories(v, ww)[0] | {"last_at": max(r["minute"] for r in v)} for k, v in groups.items()),
            key=lambda x: -x["weighted"])[:10]
        return {"kind": kind, "category": (_categories(rows, ww) or [{"category": key, "weighted": 0}])[0],
                "sessions": top(per, lambda sid: meta.get(sid, {"session_id": sid})),
                "prs": top(prs, lambda n: {k: titles[n][k] for k in ("number", "title", "state", "url")})}
    if kind == "waste":
        if key == "switches":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM agent_task_switches WHERE at >= ? ORDER BY carried_tokens DESC LIMIT 20", (start,))]
        elif key == "idle":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM agent_cache_rebuilds WHERE at >= ? AND cause = 'idle' ORDER BY tokens DESC LIMIT 20",
                (start,))]
        elif key == "repeats":
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id, SUM(repeat_reads) AS repeat_reads, SUM(repeat_tokens) AS repeat_tokens, "
                "MAX(minute) AS at FROM agent_context WHERE minute >= ? AND repeat_reads > 0 GROUP BY session_id "
                "ORDER BY SUM(repeat_tokens) DESC LIMIT 20", (start,))]
        else:
            raise LookupError(f"No such kind of waste: {key}")
        for r in rows:
            r["weighted"] = round(r.get("carried_tokens", 0) * WEIGHTS["cache_read_tokens"] if key == "switches"
                                  else r.get("tokens", r.get("repeat_tokens", 0)) * ww)
            r |= {k: v for k, v in meta.get(r["session_id"], {}).items() if k != "session_id"}
        return {"kind": kind, "key": key, "rows": rows,
                "totals": context_summary(conn, start)["task_switches" if key == "switches" else "rebuilds"]
                if key != "repeats" else None}
    raise LookupError(f"No such detail: {kind}")


def context_summary(conn: sqlite3.Connection, start: str, sessions: set[str] | None = None) -> dict[str, Any]:
    """Where context went: per category, what tool results added and what carrying them cost, priced like
    weighted() (added tokens at the cache write price, carried ones at the cache read price); and the cache
    rebuilds, by cause."""
    where, args = "minute >= ?", [start]
    if sessions is not None:
        where += f" AND session_id IN ({','.join('?' * len(sessions))})"
        args += sorted(sessions)
    write_weight = _write_weight(conn, where, args)
    cats = _categories([dict(r) for r in conn.execute(f"SELECT * FROM agent_context WHERE {where}", args)], write_weight)
    rb_where = where.replace("minute", "at")
    causes = [dict(r) | {"weighted": round(r["tokens"] * write_weight)} for r in conn.execute(
        f"SELECT cause, COUNT(*) AS rebuilds, SUM(tokens) AS tokens, COUNT(DISTINCT session_id) AS sessions, "
        f"MAX(idle_seconds) AS longest_idle FROM agent_cache_rebuilds WHERE {rb_where} GROUP BY cause "
        "ORDER BY SUM(tokens) DESC", args)]
    sw = conn.execute(f"SELECT COUNT(*), SUM(context_tokens), SUM(carried_tokens), COUNT(DISTINCT session_id) "
                      f"FROM agent_task_switches WHERE {rb_where}", args).fetchone()
    summaries = [r[0] for r in conn.execute(f"SELECT tokens FROM agent_cache_rebuilds WHERE {rb_where} "
                                            "AND cause = 'compaction' ORDER BY tokens", args)]
    return {"categories": cats, "repeat_reads": sum(c["repeat_reads"] for c in cats),
            "task_switches": {"count": sw[0], "sessions": sw[3], "context_tokens": sw[1] or 0,
                              "carried_tokens": sw[2] or 0,
                              "weighted": round((sw[2] or 0) * WEIGHTS["cache_read_tokens"]),
                              "compacted_to": summaries[len(summaries) // 2] if summaries else None},
            "repeat_tokens": sum(c["repeat_tokens"] for c in cats),
            "rebuilds": {"count": sum(c["rebuilds"] for c in causes), "tokens": sum(c["tokens"] for c in causes),
                         "weighted": sum(c["weighted"] for c in causes), "by_cause": causes}}


def _prs(conn: sqlite3.Connection, head: str | None = None) -> tuple[list[dict[str, Any]], dict[str, list[dict]]]:
    where, args = ("WHERE head = ?", (head,)) if head else ("WHERE head IS NOT NULL", ())
    prs = [dict(r) for r in conn.execute(
        f"SELECT number, title, state, head, created_at, merged_at, closed_at, url FROM pull_requests {where} "
        "ORDER BY created_at", args)]
    by_head: dict[str, list[dict]] = defaultdict(list)
    for p in prs:
        p["end"] = p["merged_at"] or p["closed_at"] or "9999"
        by_head[p["head"]].append(p)
    for heads in by_head.values():
        heads.sort(key=lambda p: p["end"])
    return prs, by_head


def _pr_for(by_head: dict[str, list[dict]], branch: str, minute: str) -> dict[str, Any] | None:
    """The pull request a minute on a branch counts toward: the first one on that head still open then."""
    return next((p for p in by_head.get(branch, []) if minute <= p["end"]), None)


def _by_pr(conn: sqlite3.Connection, rows: list) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prs, by_head = _prs(conn)
    got: dict[int, list] = defaultdict(list)
    sessions: dict[int, set] = defaultdict(set)
    loose = []
    for r in rows:
        pr = _pr_for(by_head, r["branch"], r["minute"])
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


def _red_windows(conn: sqlite3.Connection, start: str) -> list[tuple[str, str, str, str | None]]:
    """(session, test, from, to) for each time a test was red in a session: from a run there that failed it to the
    next run there that passed it (to is None when none did)."""
    ids: dict[str, str] = {}
    for sid, remote in conn.execute("SELECT session_id, remote_session FROM agent_sessions"):
        ids[sid] = sid
        if remote:
            ids[remote] = sid
    if not ids:
        return []
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
    out = []
    for sid, session_runs in per_session.items():
        red: dict[str, str] = {}
        for started, outcomes in session_runs:
            for test, outcome in outcomes.items():
                if outcome in ("fail", "error") and test not in red:
                    red[test] = started
                elif outcome == "pass" and test in red:
                    out.append((sid, test, red.pop(test), started))
        out += [(sid, test, began, None) for test, began in red.items()]
    return out


def _by_test(conn: sqlite3.Connection, rows: list, start: str) -> list[dict[str, Any]]:
    """Tokens a session spent while each test was red (see the module docstring)."""
    usage: dict[str, tuple[list, list]] = {}
    for r in sorted(rows, key=lambda r: r["minute"]):
        minutes, totals = usage.setdefault(r["session_id"], ([], []))
        minutes.append(r["minute"])
        totals.append(r)
    out: dict[str, dict[str, Any]] = {}
    for sid, test, a, b in _red_windows(conn, start):
        minutes, totals = usage.get(sid, ([], []))
        lo = bisect_left(minutes, a[:16] + ":00+00:00")
        hi = bisect_left(minutes, b[:16] + ":00+00:00") if b else len(minutes)
        t = out.setdefault(test, {"test_id": test, "times_red": 0, "still_red": 0, "sessions": set(),
                                  "requests": 0, **{f: 0 for f in FIELDS}})
        t["times_red"] += 1
        t["still_red"] += b is None
        t["sessions"].add(sid)
        for r in totals[lo:hi]:
            t["requests"] += r["requests"]
            for f in FIELDS:
                t[f] += r[f]
    tests = [t | {"sessions": len(t["sessions"]), "weighted": weighted(t)} for t in out.values()]
    return sorted(tests, key=lambda t: -t["output_tokens"])


def _test_items(conn: sqlite3.Connection, test_id: str, start: str) -> list[dict[str, Any]]:
    """What entered a session's context while this test was red there, with what it cost from then on. Arriving
    while the test was red doesn't mean it came because of the test: this is a time window, not a cause."""
    rows = []
    for sid, test, a, b in _red_windows(conn, start):
        if test != test_id:
            continue
        q = "SELECT * FROM agent_context_adds WHERE session_id = ? AND at >= ?" + (" AND at < ?" if b else "")
        rows += [dict(r) | {"adds": 1} for r in conn.execute(q, (sid, a, b) if b else (sid, a))]
    return _item_totals(rows, _write_weight(conn, "minute >= ?", (start,)))[:30]


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
        "weighted_per_day": [weighted(daily[d]) for d in days_],
        "totals": _sum(rows) | {"sessions": len(per_session)},
        "context": context_summary(conn, start),
        "items": items_summary(conn, start),
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
