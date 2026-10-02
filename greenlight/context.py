"""What a Claude Code session's tools put into its context, and what keeping it there cost.

Every request sends the whole conversation again, and with prompt caching that's a cache read of everything
so far. So a tool result is paid for once when it arrives and again, at the cache-read price, on every later
request until the session compacts: a 3,000 token test log at request 100 of 400 is read about 300 more
times, 900,000 cache-read tokens. Reading a transcript in order, this charges each tool result, by category
(test runs, builds, git, file reads, images, searches, web, each MCP server, subagents), with:

- tokens: an estimate of what the results added (text by a chars-per-token ratio measured from the session's
  own context growth, images by width x height / 750);
- carried_tokens: tokens x the later requests that re-read them before the next compaction;
- repeat reads: a file read again with the same range while the first read was still in context and nothing
  had edited it.

It also finds cache rebuilds: requests that wrote most of the context to the cache again, with the likely cause
(idle longer than the cache lasts, a compaction, a model switch). Only these counts leave the machine.
"""
from __future__ import annotations

import base64
import json
import re
import statistics
import struct
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .db import norm_time

# A Bash command is a test run when it matches this (override with [usage] test_commands in greenlight.toml)
TEST_COMMANDS = (r"\b(pytest|py\.test|tox|nox|jest|vitest|mocha|karma|rspec|phpunit|ctest|bats)\b"
                 r"|\b(go|cargo|dotnet|swift|mix|deno|bun) test\b|\bcargo nextest\b|\bplaywright test\b|\bcypress run\b"
                 r"|\b(npm|pnpm|yarn)( run)? (test|playtest)\b|\btest:[\w:-]+|\bnode \S*(test|playtest)\S*\.c?js\b"
                 r"|\bgreenlight (run|playtest)\b|\bunittest\b|\bmanage\.py test\b|\b(mvn|gradlew?)\b.*\btest\b")
BUILD_COMMANDS = (r"\b(npm|pnpm|yarn|bun)( run)? build\b|\btsc\b|\bvite build\b|\bwebpack\b|\bmake\b"
                  r"|\b(cargo|go|swift|dotnet) build\b|\b(mvn|gradlew?)\b.*\b(package|install|build|assemble)\b")
_CD = re.compile(r"^\s*(cd\s+\S+\s*(&&|;)\s*)+")
_SHELL = [("git", re.compile(r"(git|gh)\b")), ("read", re.compile(r"(cat|head|tail|less|more|nl|sed\s+-n|awk|jq|wc)\b")),
          ("search", re.compile(r"(grep|rg|ag|find|fd|ls|tree)\b")), ("web", re.compile(r"(curl|wget|http)\b"))]
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH = {"Grep", "Glob", "LS", "ToolSearch"}
CHARS_PER_TOKEN = 2.4   # measured on Claude Code transcripts; each session measures its own when it can
REBUILD_SHARE = 0.5     # a request whose cache lacks half of what the request before it sent...
REBUILD_MIN = 10_000    # ...when that was at least this many tokens, rebuilt the cache
TTL_SECONDS = {"1h": 3600, "5m": 300}
_SKIP_ATTACHMENTS = {"prompt_snapshot", "deferred_tools_record", "atis-latch"}  # kept in the file, not sent to the model


def category(tool: str, args: dict[str, Any], test: re.Pattern[str], build: re.Pattern[str]) -> str:
    if tool == "Bash":  # by what the command does: a file read through cat counts as a read
        cmd = str(args.get("command") or "")
        if test.search(cmd):
            return "test"
        if build.search(cmd):
            return "build"
        first = _CD.sub("", cmd).lstrip()
        return next((name for name, word in _SHELL if word.match(first)), "shell")
    if tool == "Read":
        return "read"
    if tool in SEARCH:
        return "search"
    if tool in EDITS:
        return "edit"
    if tool in ("WebFetch", "WebSearch"):
        return "web"
    if tool in ("Agent", "Task"):
        return "subagent"
    if tool == "Skill":
        return "skill"
    if tool.startswith("mcp__"):
        return "mcp:" + tool.split("__")[1]
    return "other"


def image_tokens(data: str) -> int:
    """width x height / 750 from a base64 PNG, JPEG, GIF or WebP header; 1,600 when it can't tell."""
    try:
        raw = base64.b64decode(data[:65536] + "=" * (-min(len(data), 65536) % 4))
    except ValueError:
        return 1600
    size = None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        size = struct.unpack(">II", raw[16:24])
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        size = struct.unpack("<HH", raw[6:10])
    elif raw[:4] == b"RIFF" and raw[12:16] == b"VP8X":
        size = (int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1)
    elif raw[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                break
            marker, length = raw[i + 1], struct.unpack(">H", raw[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                size = (w, h)
                break
            i += 2 + length
    return max(1, size[0] * size[1] // 750) if size and all(size) else 1600


def _text_size(content: Any) -> tuple[int, int]:
    """(chars of text, image tokens) in a tool result's content."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            images += image_tokens(str((block.get("source") or {}).get("data") or ""))
        else:
            chars += len(str(block.get("text") or ""))
    return chars, images


def _ts(value: Any) -> datetime | None:
    t = norm_time(value)
    return datetime.fromisoformat(t) if t else None


def read_file(path: Path, agent: str = "", test: str | None = None) -> dict[str, Any]:
    """One transcript file (a session or one subagent): its requests' usage, what its tools added, its rebuilds."""
    test_re = re.compile(test or TEST_COMMANDS)
    build_re = re.compile(BUILD_COMMANDS)
    requests: list[dict[str, Any]] = []   # API responses in order
    items: list[dict[str, Any]] = []      # what entered the context, and the request that first read it
    pending: list[dict[str, Any]] = []
    tools: dict[str, tuple[str, dict]] = {}
    seen: set[str] = set()
    window, compacted = 0, False
    in_context: dict[str, set] = defaultdict(set)  # path -> read ranges still in context, unedited
    try:
        lines = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return {"requests": [], "items": [], "rebuilds": [], "switches": [], "chars_per_token": CHARS_PER_TOKEN}
    with lines:
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or (e.get("isSidechain") and not agent):  # old transcripts kept subagents inline
                continue
            kind, msg = e.get("type"), e.get("message") if isinstance(e.get("message"), dict) else {}
            if kind == "system" and e.get("subtype") == "compact_boundary":
                window, compacted, pending = window + 1, True, []
                in_context.clear()
            elif kind == "assistant":
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools[str(block.get("id"))] = (str(block.get("name") or ""), block.get("input") or {})
                u, key = msg.get("usage"), msg.get("id") or e.get("requestId") or e.get("uuid")
                if not isinstance(u, dict) or not key or key in seen or msg.get("model") == "<synthetic>":
                    continue
                seen.add(key)
                at = norm_time(e.get("timestamp"))
                if not at:
                    continue
                cw = int(u.get("cache_creation_input_tokens") or 0)
                cw1h = int(((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")) or 0)
                requests.append({"at": at, "model": msg.get("model") or "unknown", "branch": e.get("gitBranch") or "",
                                 "session": e.get("sessionId"), "window": window, "after_compaction": compacted,
                                 "input_tokens": int(u.get("input_tokens") or 0),
                                 "output_tokens": int(u.get("output_tokens") or 0),
                                 "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                                 "cache_write_tokens": cw, "cache_write_1h_tokens": cw1h, "arrived": pending})
                for item in pending:
                    item["request"] = len(requests) - 1
                items.extend(pending)
                pending, compacted = [], False
            elif kind == "user":
                content = msg.get("content")
                for block in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        pending.append({"category": None, "chars": len(str(block.get("text") or "")), "images": 0})
                        continue
                    name, args = tools.get(str(block.get("tool_use_id")), ("", {}))
                    chars, images = _text_size(block.get("content"))
                    cat = "image" if images else category(name, args, test_re, build_re)
                    item = {"category": cat, "chars": chars, "images": images, "at": e.get("timestamp"),
                            "branch": e.get("gitBranch") or "", "repeat": False}
                    target = str(args.get("file_path") or args.get("notebook_path") or "")
                    if name == "Read" and target:
                        span = (args.get("offset"), args.get("limit"), args.get("pages"))
                        item["repeat"] = span in in_context[target]
                        in_context[target].add(span)
                    elif name in EDITS and target:
                        in_context.pop(target, None)
                    pending.append(item)
            elif kind == "attachment":
                a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
                if a.get("type") not in _SKIP_ATTACHMENTS and not str(a.get("type") or "").endswith("_record"):
                    pending.append({"category": None, "chars": len(json.dumps(a)), "images": 0})
    ratio = _chars_per_token(requests)
    for item in items:
        item["tokens"] = round(item["chars"] / ratio) + item["images"]
    return {"requests": requests, "items": items, "rebuilds": _rebuilds(requests), "switches": task_switches(requests),
            "chars_per_token": ratio}


def _context(r: dict[str, Any]) -> int:
    return r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"]


def _chars_per_token(requests: list[dict[str, Any]]) -> float:
    """The session's own ratio, from gaps where one big text result was all that arrived: the context grew by
    that result plus the previous response."""
    samples = []
    for a, b in zip(requests, requests[1:]):
        new = b["arrived"]
        text = [i for i in new if i["category"] and i["chars"] > 4000 and not i["images"]]
        rest = sum(i["chars"] for i in new if i not in text) + sum(i["images"] for i in new)
        grew = _context(b) - _context(a) - a["output_tokens"]
        if b["window"] == a["window"] and len(text) == 1 and rest < 500 and grew > 0:
            samples.append(text[0]["chars"] / grew)
    return min(6.0, max(1.5, statistics.median(samples))) if len(samples) >= 5 else CHARS_PER_TOKEN


def _rebuilds(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests whose cache no longer held most of what the request before them had sent: that part was written
    again. A big new tool result is a write too, but not a rebuild, so this looks at what was missing."""
    out = []
    for prev, r in zip(requests, requests[1:]):
        had = _context(prev)
        missing = had - r["cache_read_tokens"]
        if had < REBUILD_MIN or missing < REBUILD_SHARE * had or not r["cache_write_tokens"]:
            continue
        tokens = min(missing, r["cache_write_tokens"])  # after a compaction, only the shorter context is written
        ttl = "1h" if r["cache_write_1h_tokens"] * 2 >= r["cache_write_tokens"] else "5m"
        a, b = _ts(prev["at"]), _ts(r["at"])
        idle = int((b - a).total_seconds()) if a and b else None
        cause = ("compaction" if r["after_compaction"] else "model" if r["model"] != prev["model"]
                 else "idle" if idle is not None and idle >= TTL_SECONDS[ttl] else "other")
        out.append({"at": r["at"], "branch": r["branch"], "tokens": tokens, "ttl": ttl, "idle_seconds": idle,
                    "cause": cause})
    return out


DEFAULT_BRANCHES = {"", "HEAD", "main", "master", "develop", "dev", "trunk"}


def task_switches(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests that started work on a branch new to the window, with the context they inherited from the work
    before (the size of the last request before the switch) and what carrying it cost: that many tokens re-read
    by every request after, until a compaction. Default branches like main aren't a task of their own."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    last = None
    for i, r in enumerate(requests):
        if i == 0 or r["window"] != requests[i - 1]["window"]:
            seen, last = {r["branch"]}, None
            continue
        if r["branch"] not in seen and r["branch"] not in DEFAULT_BRANCHES and seen - DEFAULT_BRANCHES:
            last = {"at": r["at"], "from_branch": next((q["branch"] for q in reversed(requests[:i])
                                                        if q["branch"] not in DEFAULT_BRANCHES), ""),
                    "to_branch": r["branch"], "context_tokens": _context(requests[i - 1]), "carried_tokens": 0}
            out.append(last)
        seen.add(r["branch"])
        if last and r["at"] != last["at"]:
            last["carried_tokens"] += last["context_tokens"]
    return out


def _reads_cache(requests: list[dict[str, Any]], i: int) -> bool:
    """Whether request i found the context before it in the cache (a rebuild wrote it again instead)."""
    return i > 0 and requests[i]["window"] == requests[i - 1]["window"] and \
        requests[i]["cache_read_tokens"] >= REBUILD_SHARE * _context(requests[i - 1])


def carried(requests: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    """Sets carried_tokens on each item: its tokens times the later requests in the same window that read it from
    the cache. The request that first sees an item writes it; a rebuild writes it again (counted as a rebuild)."""
    after: list[int] = [0] * len(requests)
    for i in range(len(requests) - 2, -1, -1):
        same = requests[i + 1]["window"] == requests[i]["window"]
        after[i] = (after[i + 1] + _reads_cache(requests, i + 1)) if same else 0
    for item in items:
        item["carried_tokens"] = item["tokens"] * after[item["request"]] if "request" in item else 0


def rows(files: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (minute, branch, category) totals: what gets stored and sent. Besides the tool categories, two account
    for the rest of every cache read: system (what the first request of a window sent: the system prompt, tool
    definitions, CLAUDE.md, and after a compaction its summary) and conversation (Claude's own replies, the
    user's messages and reminders: what's left)."""
    out: dict[tuple, dict[str, Any]] = {}

    def add(at: str, branch: str, cat: str, calls: int = 0, tokens: int = 0, carried_tokens: int = 0,
            repeat: bool = False) -> None:
        key = (at[:16] + ":00+00:00", branch, cat)
        r = out.setdefault(key, {"minute": key[0], "branch": key[1], "category": key[2], "calls": 0, "tokens": 0,
                                 "carried_tokens": 0, "repeat_reads": 0, "repeat_tokens": 0})
        r["calls"] += calls
        r["tokens"] += tokens
        r["carried_tokens"] += carried_tokens
        if repeat:
            r["repeat_reads"] += 1
            r["repeat_tokens"] += tokens

    for f in files:
        requests = f["requests"]
        carried(requests, f["items"])
        for item in f["items"]:
            at = norm_time(item.get("at"))
            if item["category"] and at and "request" in item:
                add(at, item["branch"], item["category"], 1, item["tokens"], item["carried_tokens"], item["repeat"])
        tools_cached = base = 0  # tool tokens and system tokens each request found in the cache
        for i, r in enumerate(requests):
            if i == 0 or r["window"] != requests[i - 1]["window"]:
                tools_cached, base = 0, _context(r)  # what arrived before the window's first request is in base
                add(r["at"], r["branch"], "system", 1, base)
                continue
            if i > 1 and requests[i - 1]["window"] == requests[i - 2]["window"]:
                tools_cached += sum(it["tokens"] for it in requests[i - 1]["arrived"] if it["category"])
            add(r["at"], r["branch"], "system", carried_tokens=min(base, r["cache_read_tokens"]))
            if _reads_cache(requests, i):  # a rebuild's reads are only the system part; tools aren't carried there
                add(r["at"], r["branch"], "conversation",
                    carried_tokens=max(0, r["cache_read_tokens"] - base - tools_cached))
    return sorted(out.values(), key=lambda r: (r["minute"], r["branch"], r["category"]))
