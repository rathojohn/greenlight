"""Claude Code token usage: how many tokens went into each pull request, and into each test while it failed.

Where the numbers come from: every Claude Code session keeps a transcript (JSONL) with the usage of each API
response: model, input, output, cache reads and writes, a timestamp and the git branch the session was on.
`greenlight usage record` runs as a Stop hook (after every turn), reads the transcript and its subagents', and
stores per-minute totals. Counts, model names, branch names, timestamps, item labels and commit shas leave the
machine, and the first line of the session's first prompt as its title ([usage] titles = false keeps it here);
never code, command output or the rest of the conversation. The transcript is Claude Code's own file, not a
documented interface, so a change to its format would need a change here.

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
import re
import sqlite3
import subprocess
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import context
from .db import day_range, iso, norm_time, parse_time, since, utcnow

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens")
# What each kind of token costs next to an input token, from Claude's API prices: output 5x, a cache read 0.1x,
# a cache write 1.25x, or 2x when it's written for an hour (cache_write_1h_tokens is the part that was). A model
# with its own prices gets its own ratios (weights()); these are for one without.
WEIGHTS = {"input_tokens": 1, "output_tokens": 5, "cache_read_tokens": 0.1, "cache_write_tokens": 1.25,
           "cache_write_1h_tokens": 0.75}
# API list prices in dollars per million tokens: input, 5-minute cache write, 1-hour cache write, cache read,
# output (platform.claude.com/docs/en/about-claude/pricing, October 2026). A dated snapshot like
# claude-haiku-4-5-20251001 is priced as its model; a model missing here gets no dollars and WEIGHTS.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50), "claude-mythos-5-1": (10, 12.5, 20, 0.25, 50),
    "claude-fable-5": (10, 12.5, 20, 1, 50), "claude-mythos-5": (10, 12.5, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.2, 20), "claude-opus-5": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-8": (5, 6.25, 10, 0.5, 25), "claude-opus-4-7": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-6": (5, 6.25, 10, 0.5, 25), "claude-opus-4-5": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4": (15, 18.75, 30, 1.5, 75), "claude-sonnet-5-5": (2, 2.5, 4, 0.2, 10),
    "claude-sonnet-5": (2, 2.5, 4, 0.2, 10), "claude-sonnet-4": (3, 3.75, 6, 0.3, 15),
    "claude-haiku-4-5": (1, 1.25, 2, 0.1, 5), "claude-3-5-haiku": (0.8, 1, 1.6, 0.08, 4),
}
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


_COMMAND = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>(?:.*?<command-args>\s*([^<]*?)\s*</command-args>)?",
                      re.S)
# Anything in a title shaped like a key: a known prefix, or a long run mixing letters and digits
_SECRET = re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|AKIA)[\w-]{8,}"
                     r"|(?<![\w+=-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{32,}")
TITLE_CHARS = 80
_MENTION = re.compile(r'@"([^"]+)"|@(\S*[\\/]\S+)')  # a file mentioned by its path


def title(text: str) -> str:
    """A prompt's first line as a session's name: whitespace collapsed, anything shaped like a key redacted, cut at
    a word near 80 characters."""
    line = next((s for s in text.splitlines() if s.strip()), "")
    line = _MENTION.sub(lambda m: "@" + re.split(r"[\\/]", m.group(1) or m.group(2))[-1], line)  # @"a/b/c.zip" -> @c.zip
    line = _SECRET.sub("[redacted]", " ".join(line.split()))
    if len(line) > TITLE_CHARS:
        line = line[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",.;:") + "..."
    return line


def first_prompt(path: str | Path) -> str | None:
    """What the person first asked in a session, as its title. A slash command counts (/review 12); Claude Code's
    own notes (command output, reminders, a compaction's summary) don't."""
    try:
        lines = Path(path).open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with lines:
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if (not isinstance(e, dict) or e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary")
                    or e.get("isSidechain") or not isinstance(e.get("message"), dict)):
                continue
            content = e["message"].get("content")
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                content = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            text = str(content or "").strip()
            command = _COMMAND.search(text)
            if command:
                text = f"{command.group(1)} {command.group(2) or ''}"
            elif text.startswith(("<", "Caveat:")):
                continue
            name = title(text)
            if name:
                return name
    return None


def _made_commit(action: str, subject: str) -> bool:
    """Whether a reflog entry made a commit: a fast-forward, a checkout or a fetch only moved a ref."""
    if re.match(r"(commit|cherry-pick|revert|rebase \((pick|reword|squash|fixup)\))", action):
        return True
    return action.startswith(("merge", "pull")) and "Merge made" in subject


def reflog(root: str) -> list[dict[str, Any]]:
    """Commits made in a checkout, from the reflogs of every branch and HEAD (so a worktree's count too): sha, when,
    branch. The reflog keeps each commit's message too, but only the sha and time are used."""
    from .gitrepo import GitError, run_git
    try:
        p = run_git(["-C", root, "reflog", "--all", "-n", "5000", "--date=unix", "--format=%H%x09%gd%x09%gs"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except (GitError, OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in p.stdout.splitlines() if p.returncode == 0 else []:
        parts = line.split("\t", 2)
        ref = re.match(r"(.+)@\{(\d+)\}$", parts[1]) if len(parts) == 3 else None
        if not ref or not _made_commit(parts[2].split(":", 1)[0], parts[2]):
            continue
        name = ref.group(1)
        out.append({"sha": parts[0], "at": datetime.fromtimestamp(int(ref.group(2)), timezone.utc),
                    "branch": "" if name == "HEAD" or name.startswith(("refs/remotes/", "origin/")) else name.removeprefix("refs/heads/")})
    return out


def commits(actions: list[dict[str, Any]], root: str | None) -> list[dict[str, Any]]:
    """The commits a session made and the pull requests it merged. A commit is whatever the reflog shows made
    during one of the session's git commands (a second either side for the clocks' rounding), on the branch the
    reflog names, else the one the session was on."""
    out = [{k: a.get(k) for k in ("kind", "at", "sha", "pr", "branch")} for a in actions if a["kind"] == "merge"]
    windows = [(parse_time(a["from"]) - timedelta(seconds=2), parse_time(a["to"]) + timedelta(seconds=2), a["branch"])
               for a in actions if a["kind"] == "commit"]
    if not windows or not root:
        return out
    seen = set()
    for c in sorted(reflog(root), key=lambda c: (c["at"], c["branch"] == "")):
        w = next((w for w in windows if w[0] <= c["at"] <= w[1]), None)
        if w and c["sha"] not in seen:
            seen.add(c["sha"])
            out.append({"kind": "commit", "at": iso(c["at"]), "sha": c["sha"], "pr": None, "branch": c["branch"] or w[2]})
    return out


def instructions_then(root: str | None) -> Callable[[str], list[tuple[str, int]]]:
    """CLAUDE.md files as the checkout had them at a time: the ones git tracks from the commit HEAD was on then (its
    reflog), or from disk when that's the commit it's on now, so an edit not yet committed counts; the rest from disk.
    For what a transcript doesn't say: the size a session started with."""
    now = context.instruction_files(root)
    if not root:
        return lambda at: now
    from .gitrepo import GitError, run_git

    def git(*args: str) -> subprocess.CompletedProcess:
        return run_git(["-C", root, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    try:
        log = [(int(m.group(2)), m.group(1)) for line in git("reflog", "HEAD", "-n", "5000", "--date=unix",
                                                             "--format=%H%x09%gd").stdout.splitlines()
               if (m := re.match(r"([0-9a-f]{40})\tHEAD@\{(\d+)\}$", line))]
        tracked = set(git("ls-files", "--", *context.INSTRUCTION_FILES).stdout.split())
    except (GitError, OSError, subprocess.TimeoutExpired):
        return lambda at: now
    seen: dict[str, list[tuple[str, int]]] = {}

    def then(at: str) -> list[tuple[str, int]]:
        t = parse_time(at)
        sha = next((h for ts, h in log if t and ts <= t.timestamp()), log[-1][1] if log else None)  # newest first
        if not sha or sha == log[0][1]:
            return now
        if sha not in seen:
            out = []
            for rel in context.INSTRUCTION_FILES:
                if rel not in tracked:
                    out += [f for f in now if f[0] == rel]
                    continue
                try:
                    p = git("cat-file", "-p", f"{sha}:{rel}")
                except (GitError, OSError, subprocess.TimeoutExpired):
                    return now
                if p.returncode == 0:
                    out.append((rel, len(p.stdout)))
            seen[sha] = out + [f for f in now if f[0] not in context.INSTRUCTION_FILES]
        return seen[sha]
    return then


def _instructions(files: list[dict[str, Any]], root: str | None) -> list[dict[str, Any]]:
    """Every window's CLAUDE.md sizes: what its transcript says, else what the checkout had when it started."""
    then = None
    for f in files:
        known = f.setdefault("instructions", {})
        for r in f["requests"]:
            if r["window"] not in known:
                then = then or instructions_then(root)
                known[r["window"]] = then(r["at"])
    return files


def price(model: str | None) -> tuple[float, ...] | None:
    """A model's API prices per million tokens (input, 5-minute write, 1-hour write, cache read, output), or None."""
    m = (model or "").lower()
    # a dated snapshot or a context tag is the same model; claude-opus-5-7 isn't claude-opus-5
    best = next((k for k in PRICES if m.startswith(k) and re.fullmatch(r"(-\d{8})?(\[\w+\])?", m[len(k):])), None)
    return PRICES[best] if best else None


def weights(model: str | None) -> dict[str, float]:
    """What each kind of token costs next to an input token of the same model."""
    p = price(model)
    if not p:
        return WEIGHTS
    inp, cw, cw1h, cr, out = p
    return {"input_tokens": 1, "output_tokens": out / inp, "cache_read_tokens": cr / inp, "cache_write_tokens": cw / inp,
            "cache_write_1h_tokens": (cw1h - cw) / inp}


def _weigh(row: dict[str, Any], model: str | None = None) -> float:
    return sum((row.get(k) or 0) * w for k, w in weights(model or row.get("model")).items())


def weighted(row: dict[str, Any], model: str | None = None) -> int:
    """Tokens priced as input tokens of their model (the row's own, unless given): what the row cost, in a unit every
    model shares."""
    return round(_weigh(row, model))


def dollars(row: dict[str, Any], model: str | None = None) -> float | None:
    """What the row's tokens cost at its model's API list price, or None for a model without one."""
    p = price(model or row.get("model"))
    if not p:
        return None
    inp, cw, cw1h, cr, out = p
    g = lambda k: row.get(k) or 0  # noqa: E731
    return (g("input_tokens") * inp + g("output_tokens") * out + g("cache_read_tokens") * cr
            + (g("cache_write_tokens") - g("cache_write_1h_tokens")) * cw + g("cache_write_1h_tokens") * cw1h) / 1e6


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
    labels = titles = True
    root = hook.get("cwd")
    if root:
        from . import config
        repo = config.remote_repo(root)
        try:
            cfg = config.load(start=Path(root))
            test, labels = cfg.get("usage", "test_commands"), cfg.get("usage", "item_labels") is not False
            titles = cfg.get("usage", "titles") is not False
        except (ValueError, OSError):
            pass
    files = read_files(transcript, test, root)
    me = agent_session(env)
    session_id = hook.get("session_id") or me["session_id"] or Path(transcript).stem
    remote = me["remote_session"] if me["session_id"] in (None, session_id) else None  # only for this session
    return {"session": {"session_id": session_id, "remote_session": remote, "repo": repo,
                        "agent": "claude-code", "title": first_prompt(transcript) if titles else None},
            "rows": minute_rows(entries(files)), "context": context.rows(files),
            "rebuilds": [r | {"agent": f["agent"]} for f in files for r in f["rebuilds"]],
            "switches": [r | {"agent": f["agent"]} for f in files for r in f["switches"]],
            "commits": commits([a for f in files for a in f["actions"]], root),
            **({"items": context.item_rows(_instructions(files, root)), "adds": context.add_rows(files)}
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
    extra = ([payload.get(k) for k in ("context", "rebuilds", "switches", "items", "adds", "commits")]
             if isinstance(payload, dict) else [None] * 6)
    if (not isinstance(s, dict) or not s.get("session_id") or not isinstance(rows, list) or len(rows) > MAX_ROWS
            or any(not isinstance(x, (list, type(None))) or len(x or ()) > MAX_ROWS for x in extra)):
        raise ValueError("send {\"session\": {\"session_id\": ...}, \"rows\": [...]}")
    ctx, rebuilds, switches, items, adds, made = extra
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
    commit_rows = [(sid, _minute(r), r["kind"], str(r.get("sha") or "").lower(),
                    int(r["pr"]) if str(r.get("pr") or "").isdigit() else None, str(r.get("branch") or ""))
                   for r in made or () if isinstance(r, dict) and r.get("kind") in ("commit", "merge")
                   and re.fullmatch(r"[0-9a-fA-F]{7,40}|", str(r.get("sha") or ""))]
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
        if made is not None:
            conn.execute("DELETE FROM agent_commits WHERE session_id = ?", (sid,))
            conn.executemany("INSERT OR REPLACE INTO agent_commits (session_id, at, kind, sha, pr, branch) "
                             "VALUES (?,?,?,?,?,?)", commit_rows)
        # an older client sends no title: keep what's there. A newer one sends null when titles are off: clear it
        name = (title(s["title"]) or None) if isinstance(s.get("title"), str) else None
        conn.execute(
            """INSERT INTO agent_sessions (session_id, remote_session, repo, agent, first_at, last_at, updated_at, title)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET remote_session = COALESCE(excluded.remote_session, remote_session),
                 repo = COALESCE(excluded.repo, repo), first_at = excluded.first_at, last_at = excluded.last_at,
                 updated_at = excluded.updated_at, title = CASE WHEN ? THEN excluded.title ELSE title END""",
            (sid, s.get("remote_session"), s.get("repo"), s.get("agent") or "claude-code", first, last,
             iso(utcnow()), name, "title" in s))
    return {"session_id": sid, "minutes": len(clean)}


def _sum(rows: list) -> dict[str, Any]:
    """Usage rows added up, each priced by its own model."""
    out = {"requests": 0, **{f: 0 for f in FIELDS}}
    cost = reads = usd = 0.0
    for r in rows:
        out["requests"] += r["requests"]
        for f in FIELDS:
            out[f] += r[f]
        cost += _weigh(r)
        reads += r["cache_read_tokens"] * weights(r.get("model"))["cache_read_tokens"]
        usd += dollars(r) or 0
    return out | {"weighted": round(cost), "cache_read_weighted": round(reads), "dollars": round(usd, 2)}


def _rates(conn: sqlite3.Connection, where: str = "1", args: tuple | list = ()) -> dict[str, float]:
    """What a cache write and a cache read cost here on average, by the models that made them: next to an input
    token (a write is 1.25x, up to 2x as more of them last an hour), and in dollars per token. Context tables keep
    tokens without a model, so they're priced at these."""
    write = read = writes = reads = write_usd = read_usd = priced_writes = priced_reads = 0.0
    for model, cw, cw1h, cr in conn.execute(
            f"SELECT model, SUM(cache_write_tokens), SUM(cache_write_1h_tokens), SUM(cache_read_tokens) FROM agent_usage "
            f"WHERE {where} GROUP BY model", args):
        cw, cw1h, cr = cw or 0, cw1h or 0, cr or 0
        w = weights(model)
        write += cw * w["cache_write_tokens"] + cw1h * w["cache_write_1h_tokens"]
        read += cr * w["cache_read_tokens"]
        writes, reads = writes + cw, reads + cr
        p = price(model)
        if p:
            write_usd += ((cw - cw1h) * p[1] + cw1h * p[2]) / 1e6
            read_usd += cr * p[3] / 1e6
            priced_writes, priced_reads = priced_writes + cw, priced_reads + cr
    return {"write": write / writes if writes else WEIGHTS["cache_write_tokens"] + WEIGHTS["cache_write_1h_tokens"],
            "read": read / reads if reads else WEIGHTS["cache_read_tokens"],
            "write_usd": write_usd / priced_writes if priced_writes else 0.0,
            "read_usd": read_usd / priced_reads if priced_reads else 0.0}


def _context_cost(r: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    """Tokens that entered the context, at the cache write price, and their re-reads at the cache read price."""
    return {"weighted": round(r["tokens"] * rates["write"] + r["carried_tokens"] * rates["read"]),
            "dollars": round(r["tokens"] * rates["write_usd"] + r["carried_tokens"] * rates["read_usd"], 2)}


def _categories(rows: list[dict[str, Any]], rates: dict[str, float]) -> list[dict[str, Any]]:
    cats: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = cats.setdefault(r["category"], {"category": r["category"], "sessions": set(), **{f: 0 for f in CONTEXT_FIELDS}})
        c["sessions"].add(r["session_id"])
        for f in CONTEXT_FIELDS:
            c[f] += r[f]
    out = [c | {"sessions": len(c["sessions"])} | _context_cost(c, rates) for c in cats.values()]
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
    rates = _rates(conn, where.replace("seg_start", "minute"), args)  # before the filter on items' own columns
    if where_extra:
        where += f" AND {where_extra}"
        args += list(args_extra)
    sums = ", ".join(f"SUM({f}) AS {f}" for f in ("adds", "tokens", "rides", "carried_tokens"))

    def rows(by: str) -> list[dict[str, Any]]:
        out = []
        for r in conn.execute(f"SELECT {by}, kind, COUNT(DISTINCT session_id) AS sessions, {sums}, MAX(max_rides) AS "
                              f"max_rides, COUNT(DISTINCT label) AS labels FROM agent_context_items WHERE {where} "
                              f"GROUP BY {by}, kind", args):
            d = dict(r) | _context_cost(dict(r), rates)
            d["avg_rides"] = round(d["rides"] / d["adds"], 1) if d["adds"] else 0
            out.append(d)
        return sorted(out, key=lambda d: -d["weighted"])
    return {"items": rows("label")[:50], "groups": [g for g in rows("grp") if g["labels"] > 1][:20]}


def _item_totals(rows: list[dict[str, Any]], rates: dict[str, float]) -> list[dict[str, Any]]:
    """Item rows summed per (label, kind), priced like items_summary."""
    out: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        t = out.setdefault((r["label"], r["kind"]), {"label": r["label"], "kind": r["kind"], "sessions": set(), "adds": 0,
                                                     "tokens": 0, "rides": 0, "carried_tokens": 0, "max_rides": 0})
        t["sessions"].add(r["session_id"])
        for f in ("adds", "tokens", "rides", "carried_tokens"):
            t[f] += r.get(f) or 0
        t["max_rides"] = max(t["max_rides"], r.get("max_rides") or r.get("rides") or 0)
    return sorted(({**t, "sessions": len(t["sessions"]), **_context_cost(t, rates),
                    "avg_rides": round(t["rides"] / t["adds"], 1) if t["adds"] else 0} for t in out.values()),
                  key=lambda t: -t["weighted"])


def _meta(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out = {}
    for r in conn.execute("SELECT session_id, remote_session, repo, title FROM agent_sessions"):
        out[r[0]] = {"session_id": r[0], "remote_session": r[1], "repo": r[2], "title": r[3],
                     "url": f"https://claude.ai/code/{r[1]}" if r[1] else None}
    return out


def detail(conn: sqlite3.Connection, days: int, kind: str, key: str) -> dict[str, Any]:
    """One thing on the Token usage page, for its side panel: a session, a pull request, a test, a context
    category, or one kind of context that could have been dropped (switches, idle, repeats)."""
    from . import commits
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
                    "WHERE session_id = ? ORDER BY at", (sid,))],
                "commits": commits.for_session(conn, sid)}
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
                "context": {"categories": _categories(ctx, _rates(conn))}, "sessions": sessions,
                "items": _item_totals(item_rows, _rates(conn))[:30]} | commits.for_pr(conn, int(key), days)
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
    rates = _rates(conn, "minute >= ?", (start,))
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
            (head(k) | _categories(v, rates)[0] | {"last_at": max(r["minute"] for r in v)} for k, v in groups.items()),
            key=lambda x: -x["weighted"])[:10]
        return {"kind": kind, "category": (_categories(rows, rates) or [{"category": key, "weighted": 0, "dollars": 0}])[0],
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
            r |= _context_cost({"tokens": 0, "carried_tokens": r.get("carried_tokens", 0)} if key == "switches"
                               else {"tokens": r.get("tokens", r.get("repeat_tokens", 0)), "carried_tokens": 0}, rates)
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
    rates = _rates(conn, where, args)
    cats = _categories([dict(r) for r in conn.execute(f"SELECT * FROM agent_context WHERE {where}", args)], rates)
    rb_where = where.replace("minute", "at")
    causes = [dict(r) | _context_cost({"tokens": r["tokens"], "carried_tokens": 0}, rates) for r in conn.execute(
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
                              **_context_cost({"tokens": 0, "carried_tokens": sw[2] or 0}, rates),
                              "compacted_to": summaries[len(summaries) // 2] if summaries else None},
            "repeat_tokens": sum(c["repeat_tokens"] for c in cats),
            "rebuilds": {"count": sum(c["rebuilds"] for c in causes), "tokens": sum(c["tokens"] for c in causes),
                         "weighted": sum(c["weighted"] for c in causes),
                         "dollars": round(sum(c["dollars"] for c in causes), 2), "by_cause": causes}}


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
                                  "requests": 0, **{f: 0 for f in FIELDS}, "weighted": 0.0, "dollars": 0.0})
        t["times_red"] += 1
        t["still_red"] += b is None
        t["sessions"].add(sid)
        for r in totals[lo:hi]:
            t["requests"] += r["requests"]
            for f in FIELDS:
                t[f] += r[f]
            t["weighted"] += _weigh(r)
            t["dollars"] += dollars(r) or 0
    tests = [t | {"sessions": len(t["sessions"]), "weighted": round(t["weighted"]), "dollars": round(t["dollars"], 2)}
             for t in out.values()]
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
    return _item_totals(rows, _rates(conn, "minute >= ?", (start,)))[:30]


def summary(conn: sqlite3.Connection, days: int = 30, pr: int | None = None,
            test_id: str | None = None) -> dict[str, Any]:
    start = since(days)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM agent_usage WHERE minute >= ? ORDER BY minute", (start,))]
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {f: 0 for f in (*FIELDS, "weighted", "dollars")})
    models: dict[str, list] = defaultdict(list)
    per_session: dict[str, list] = defaultdict(list)
    for r in rows:
        day = daily[r["minute"][:10]]
        for f in FIELDS:
            day[f] += r[f]
        day["weighted"] += _weigh(r)
        day["dollars"] += dollars(r) or 0
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
        sessions.append({"session_id": sid, "remote_session": remote, "repo": m.get("repo"), "title": m.get("title"),
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
        "weighted_per_day": [round(daily[d]["weighted"]) for d in days_],
        "dollars_per_day": [round(daily[d]["dollars"], 2) for d in days_],
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
