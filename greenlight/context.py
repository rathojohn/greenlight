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
(idle longer than the cache lasts, a compaction, a model switch).

And it follows each thing that entered the context on its own (item_rows): a file, a screenshot, a command's
output, a skill, CLAUDE.md. For each, how many requests it rode along with and what that cost: a screenshot read
early in a session can be re-read by the next 85 requests. CLAUDE.md is in every request of every session and
subagent, so it's measured from disk and charged to all of them. Items are named by a label only: a path from
the repo root, a skill's name, or a command's program and subcommand (never its arguments or output).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import statistics
import struct
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

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
# Programs whose subcommand says what ran (npm run test, git diff); for any other program only its name is kept,
# so an argument (a token, a URL, a message) never ends up in a label
MULTI_WORD = {"npm", "pnpm", "yarn", "bun", "deno", "npx", "node", "python", "python3", "uv", "uvx", "pip", "git", "gh",
              "cargo", "go", "make", "docker", "kubectl", "greenlight", "dotnet", "mvn", "gradle", "gradlew", "swift",
              "pytest", "ruby", "bundle", "rake", "php", "composer", "tsc", "vite", "jest", "vitest", "playwright"}
# Notes Claude Code adds to the conversation on its own, by attachment type
REMINDERS = {"task_reminder": "task list reminders", "total_tokens_reminder": "token count notes",
             "queued_command": "messages sent mid-turn", "silent_turn_reminder": "turn reminders",
             "edited_text_file": "notes on edited files", "todo_reminder": "todo list reminders",
             "deferred_tools_delta": "tool list changes", "agent_listing_delta": "agent list changes",
             "date_change": "date changes", "plan_mode": "plan mode notes", "hook_additional_context": "hook output"}
INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")  # from the repo root; and ~/.claude/CLAUDE.md


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


def command_label(cmd: str) -> str:
    """A shell command named by what ran, without its arguments: `cd x && npm run test:changed -- --dry` is
    "npm run test:changed", `curl -H 'Authorization: ...' https://...` is "curl"."""
    first = re.split(r"\s*(?:\|\||&&|\||;|\n)\s*", _CD.sub("", cmd).strip(), maxsplit=1)[0]
    try:
        words = shlex.split(first, posix=True)
    except ValueError:
        words = first.split()
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in ("env", "sudo", "time", "exec")):
        words.pop(0)
        while words and words[0].startswith("-"):  # env -u NAME, timeout 900
            words = words[2:] if words[0] in ("-u", "--unset") else words[1:]
    if words and words[0] == "timeout":
        words = words[2:]
    if not words:
        return "shell"
    out = [os.path.basename(words[0])]
    if out[0] in MULTI_WORD:
        for w in words[1:3]:
            if w.startswith("-") or re.search(r"[=<>&$`'\"]", w) or "://" in w or (("/" in w or "." in w)
                                                               and not re.search(r"\.(c?js|mjs|ts|py|sh|rb)$", w)):
                break
            out.append(w)
    return " ".join(out)[:80]


def _rel(path: str, root: str | None) -> str:
    """A path from the repo root when it's inside it, ~ for the home folder, else its last two parts."""
    p = str(path or "").replace("\\", "/")
    if root:
        r = str(root).replace("\\", "/").rstrip("/") + "/"
        if p.startswith(r):
            return p[len(r):]
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/") + "/"
    if p.startswith(home):
        return "~/" + p[len(home):]
    parts = [x for x in p.split("/") if x]
    return ".../" + "/".join(parts[-2:]) if len(parts) > 2 else p


def _group(rel: str) -> str:
    """Files that belong together: same folder, same extension (tools/playtest/out/*.png)."""
    head, _, name = rel.rpartition("/")
    ext = name.rsplit(".", 1)[1] if "." in name else ""
    return f"{head + '/' if head else ''}*{'.' + ext if ext else ''}"


def identity(tool: str, args: dict[str, Any], images: int, root: str | None) -> tuple[str, str, str] | None:
    """(label, kind, group) for a tool result, or None for one too small to follow (an edit's confirmation)."""
    if tool == "Read" and args.get("file_path"):
        rel = _rel(args["file_path"], root)
        return rel, "image" if images else "file", _group(rel)
    if tool == "Bash":
        label = command_label(str(args.get("command") or ""))
        return label, "command", label.split(" ")[0]
    if tool in EDITS or tool in ("Skill", "TodoWrite", "TaskCreate", "TaskUpdate"):
        return None
    if tool == "WebFetch":
        host = urlparse(str(args.get("url") or "")).hostname or "web"
        return f"WebFetch {host}", "web", "WebFetch"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"{parts[1]}: {'__'.join(parts[2:])}", "image" if images else "mcp", f"MCP {parts[1]}"
    if tool in ("Agent", "Task"):
        return f"subagent: {args.get('subagent_type') or 'general-purpose'}", "subagent", "subagents"
    return tool, "image" if images else "tool", tool


def instruction_files(root: str | None) -> list[tuple[str, int]]:
    """CLAUDE.md files Claude Code loads into every session here: (label, chars)."""
    out = []
    for rel in INSTRUCTION_FILES if root else ():
        f = Path(root) / rel
        if f.is_file():
            out.append((rel, len(f.read_text(encoding="utf-8", errors="replace"))))
    user = Path(os.path.expanduser("~/.claude/CLAUDE.md"))
    if user.is_file():
        out.append(("~/.claude/CLAUDE.md", len(user.read_text(encoding="utf-8", errors="replace"))))
    return out


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


def read_file(path: Path, agent: str = "", test: str | None = None, root: str | None = None) -> dict[str, Any]:
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
                            "branch": e.get("gitBranch") or "", "repeat": False, "who": identity(name, args, images, root)}
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
                    pending.extend(_attachment(a, e, root))
    ratio = _chars_per_token(requests)
    for item in items:
        item["tokens"] = round(item["chars"] / ratio) + item["images"]
    return {"requests": requests, "items": items, "rebuilds": _rebuilds(requests), "switches": task_switches(requests),
            "chars_per_token": ratio}


def _attachment(a: dict[str, Any], e: dict[str, Any], root: str | None) -> list[dict[str, Any]]:
    """What an attachment put into the context, as items. CLAUDE.md re-read after a compaction is left to
    item_rows, which charges it to every request from the file on disk."""
    base = {"category": None, "images": 0, "at": e.get("timestamp"), "branch": e.get("gitBranch") or ""}
    kind = a.get("type")
    if kind == "invoked_skills":
        return [base | {"chars": len(str(sk.get("content") or "")), "who": (f"skill: {sk.get('name')}", "skill", "skills")}
                for sk in a.get("skills") or [] if isinstance(sk, dict)]
    if kind == "skill_listing":
        return [base | {"chars": len(str(a.get("content") or "")), "who": ("skill list", "skill", "skills")}]
    if kind == "file" and a.get("filename"):
        rel = a.get("displayPath") or _rel(a["filename"], root)
        return [base | {"chars": len(json.dumps(a.get("content") or "")), "who": (rel, "file", _group(rel))}]
    if kind == "mcp_instructions_delta":
        return [base | {"chars": len(json.dumps(a.get("addedBlocks") or [])), "who": ("MCP server instructions",
                                                                                     "instructions", "instructions")}]
    if kind == "instructions":
        return [base | {"chars": len(json.dumps(a))}]
    label = REMINDERS.get(kind) or str(kind or "notice").replace("_", " ")
    return [base | {"chars": len(json.dumps(a)), "who": (label, "reminder", "reminders")}]


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
        item["rides"] = after[item["request"]] if "request" in item else 0
        item["carried_tokens"] = item["tokens"] * item["rides"]


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


ITEM_FIELDS = ("adds", "tokens", "rides", "max_rides", "carried_tokens")


def item_rows(files: list[dict[str, Any]], instructions: Iterable[tuple[str, int]] = (), limit: int = 300) -> list[dict]:
    """Per (label, kind): how many times it entered the context (adds), its tokens over all of them, the requests
    that read it from the cache after (rides, and the most for one add), and the cache reads that cost. The
    largest `limit` by cost, CLAUDE.md always."""
    out: dict[tuple, dict[str, Any]] = {}

    def add(label: str, kind: str, group: str, at: str | None, branch: str, adds: int, tokens: int, rides: int,
            longest: int, carried_tokens: int) -> None:
        r = out.setdefault((label, kind), {"label": label, "kind": kind, "group": group, "first_at": at, "branch": branch,
                                           **{f: 0 for f in ITEM_FIELDS}})
        r["adds"] += adds
        r["tokens"] += tokens
        r["rides"] += rides
        r["max_rides"] = max(r["max_rides"], longest)
        r["carried_tokens"] += carried_tokens

    for f in files:
        carried(f["requests"], f["items"])
        for item in f["items"]:
            if item.get("who") and "request" in item:
                label, kind, group = item["who"]
                add(label, kind, group, norm_time(item.get("at")), item["branch"], 1, item["tokens"], item["rides"],
                    item["rides"], item["carried_tokens"])
    rows = sorted(out.values(), key=lambda r: -r["carried_tokens"])[:limit]
    started = [f["requests"][0] for f in files if f["requests"]]
    if started:
        reads = writes = longest = 0
        for f in files:
            per_window: dict[int, int] = defaultdict(int)
            for i, r in enumerate(f["requests"]):
                if _reads_cache(f["requests"], i):
                    reads += 1
                    per_window[r["window"]] += 1
                else:
                    writes += 1
            longest = max([longest, *per_window.values()])
        ratio = files[0].get("chars_per_token") or CHARS_PER_TOKEN
        for label, chars in instructions:
            t = round(chars / ratio)
            rows.append({"label": label, "kind": "instructions", "group": "instructions", "first_at": started[0]["at"],
                         "branch": started[0]["branch"], "adds": writes, "tokens": t * writes, "rides": reads,
                         "max_rides": longest, "carried_tokens": t * reads})
    return rows
