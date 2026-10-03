"""Where a session's context went: tool results by category, what carrying them cost, repeat reads, cache rebuilds
and task switches, read from a transcript whose usage numbers are set by hand."""
import base64
import json
import struct
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from greenlight import context, usage
from greenlight.db import connect

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=6)
KB = "x" * 2400  # 1,000 tokens at the default 2.4 chars per token


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def req(minutes: float, msg_id: str, read: int, write: int, branch: str = "fix", tools: tuple = (), model: str = "m",
        out: int = 10) -> str:
    content = [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in tools] or [{"type": "text"}]
    return json.dumps({"type": "assistant", "timestamp": ts(minutes), "gitBranch": branch, "sessionId": "s",
                       "message": {"id": msg_id, "model": model, "content": content,
                                   "usage": {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": read,
                                             "cache_creation_input_tokens": write,
                                             "cache_creation": {"ephemeral_1h_input_tokens": write}}}})


def res(minutes: float, tool_id: str, body, branch: str = "fix") -> str:
    return json.dumps({"type": "user", "timestamp": ts(minutes), "gitBranch": branch,
                       "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": body}]}})


def png(w: int, h: int) -> dict:
    head = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(head).decode()}}


def transcript(tmp_path):
    lines = [
        req(0, "r1", 0, 20_000, tools=(("t1", "Bash", {"command": "cd app && npm test 2>&1 | tail -40"}),
                                       ("t2", "Read", {"file_path": "/a.py"}))),
        res(0.1, "t1", KB), res(0.1, "t2", [{"type": "text", "text": KB}]),
        req(1, "r2", 20_000, 2_010, tools=(("t3", "Read", {"file_path": "/a.py"}), ("t4", "Edit", {"file_path": "/a.py"}))),
        req(1, "r2", 20_000, 2_010),  # the same response's next content block: one request
        res(1.1, "t3", KB), res(1.1, "t4", "ok"),
        req(2, "r3", 22_010, 1_100, tools=(("t5", "Read", {"file_path": "/a.py"}), ("t6", "Bash", {"command": "cat b.txt"}),
                                          ("t7", "mcp__github__get_pr", {}), ("t8", "Read", {"file_path": "/shot.png"}))),
        res(2.1, "t5", KB), res(2.1, "t6", KB), res(2.1, "t7", KB), res(2.1, "t8", [png(750, 100)]),
        req(3, "r4", 23_110, 3_100),
        req(70, "r5", 0, 26_210),  # back after 67 minutes: the hour-long cache is gone
        json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts(70.5)}),
        req(71, "r6", 0, 5_000),
        req(72, "r7", 5_000, 100, branch="next-task", tools=(("t9", "Bash", {"command": "git status"}),)),
        res(72.1, "t9", KB, branch="next-task"),
        req(73, "r8", 5_100, 1_000, branch="next-task"),
        req(74, "r9", 6_100, 10, branch="next-task"),
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_tool_results_by_category_with_what_carrying_them_cost(tmp_path):
    f = context.read_file(transcript(tmp_path))
    assert len(f["requests"]) == 9 and f["chars_per_token"] == context.CHARS_PER_TOKEN
    rows = {r["category"]: r for r in context.rows([f]) if r["category"] not in ("system", "conversation")}
    by = {}
    for r in context.rows([f]):
        b = by.setdefault(r["category"], {"calls": 0, "tokens": 0, "carried_tokens": 0, "repeat_reads": 0})
        for k in b:
            b[k] += r[k]
    assert by["test"] == {"calls": 1, "tokens": 1000, "carried_tokens": 2000, "repeat_reads": 0}  # r3, r4; r5 rebuilt
    assert by["read"]["calls"] == 4  # three Reads and a cat
    assert by["read"]["repeat_reads"] == 1  # the second Read of /a.py; the one after the edit is not a repeat
    assert by["image"]["tokens"] == 100  # 750 x 100 / 750
    assert by["mcp:github"]["calls"] == 1 and by["git"]["calls"] == 1 and by["edit"]["calls"] == 1
    assert by["git"]["carried_tokens"] == 1000  # after the compaction: r8 writes it, r9 reads it
    assert "fix" in {r["branch"] for r in rows.values()}


def test_system_and_conversation_account_for_every_cache_read(tmp_path):
    f = context.read_file(transcript(tmp_path))
    rows = context.rows([f])
    carried = sum(r["carried_tokens"] for r in rows)
    reads = sum(r["cache_read_tokens"] for r in f["requests"])
    assert carried == reads
    system = sum(r["tokens"] for r in rows if r["category"] == "system")
    assert system == 20_001 + 5_001  # the first request of each window: before and after the compaction


def test_cache_rebuilds_and_task_switches(tmp_path):
    f = context.read_file(transcript(tmp_path))
    rebuilds = {r["cause"]: r for r in f["rebuilds"]}
    assert rebuilds["idle"]["tokens"] == 26_210 and rebuilds["idle"]["ttl"] == "1h"
    assert rebuilds["idle"]["idle_seconds"] == 67 * 60
    assert rebuilds["compaction"]["tokens"] == 5_000  # only the shorter context is written again
    assert len(f["rebuilds"]) == 2  # a big new tool result (r3's 1,100) is a write, not a rebuild
    [switch] = f["switches"]
    assert switch["from_branch"] == "fix" and switch["to_branch"] == "next-task"
    assert switch["context_tokens"] == 5_001 and switch["carried_tokens"] == 2 * 5_001  # r8 and r9 read it


def test_shell_commands_count_by_what_they_do():
    t, b = __import__("re").compile(context.TEST_COMMANDS), __import__("re").compile(context.BUILD_COMMANDS)
    cat = lambda c: context.category("Bash", {"command": c}, t, b)  # noqa: E731
    assert [cat(c) for c in ("pytest -q", "npm run test:changed -- --dry", "node tools/playtest/run.cjs smoke",
                             "greenlight run -- npm test", "cargo test", "npm run build", "cd x && git log -3",
                             "sed -n 1,80p a.py", "rg foo", "curl -s https://x", "python3 make_thing.py")] == [
        "test", "test", "test", "test", "test", "build", "git", "read", "search", "web", "shell"]
    assert context.category("Skill", {}, t, b) == "skill" and context.category("Agent", {}, t, b) == "subagent"


def test_the_hook_sends_context_and_the_server_keeps_it(tmp_path):
    path = transcript(tmp_path)
    payload = usage.payload_from_hook({"transcript_path": str(path), "session_id": "s"}, {})
    assert payload["context"] and payload["rebuilds"] and payload["switches"]
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        usage.store(conn, payload)
        s = usage.summary(conn, 3)
        cats = {c["category"]: c for c in s["context"]["categories"]}
        assert cats["test"]["carried_tokens"] == 2000 and cats["test"]["sessions"] == 1
        assert abs(sum(c["share"] for c in cats.values()) - 1) < 0.01
        assert s["context"]["rebuilds"]["count"] == 2 and s["context"]["task_switches"]["count"] == 1
        assert s["context"]["task_switches"]["compacted_to"] == 5_000
        assert s["totals"]["cache_write_1h_tokens"] == s["totals"]["cache_write_tokens"]
        assert s["totals"]["weighted"] == usage.weighted(s["totals"])
        assert usage.weighted({"output_tokens": 1, "cache_read_tokens": 10, "cache_write_tokens": 4,
                               "cache_write_1h_tokens": 4}) == 5 + 1 + 8
        usage.store(conn, {"session": {"session_id": "s"}, "rows": payload["rows"]})  # an older hook: no context
        assert conn.execute("SELECT COUNT(*) FROM agent_context").fetchone()[0] == len(payload["context"])


def test_chars_per_token_comes_from_the_session_when_it_can():
    reqs = []
    for i in range(7):  # each gap: one 9,000 char result, and the context grew by it plus the last response
        reqs.append({"window": 0, "input_tokens": 0, "cache_read_tokens": i * 3010, "cache_write_tokens": 0,
                     "output_tokens": 10, "arrived": [{"category": "read", "chars": 9000, "images": 0}] if i else []})
    assert context._chars_per_token(reqs) == 3.0


def test_side_panel_details_for_a_session_a_pr_a_category_and_waste(tmp_path):
    payload = usage.payload_from_hook({"transcript_path": str(transcript(tmp_path)), "session_id": "s"},
                                      {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01X"})
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        conn.execute("INSERT INTO pull_requests (number, title, state, head, created_at, merged_at) VALUES "
                     "(7, 'Fix it', 'merged', 'fix', ?, ?)", (ts(-10).replace("Z", "+00:00"), ts(71.5).replace("Z", "+00:00")))
        usage.store(conn, payload)
        s = usage.detail(conn, 3, "session", "session_01X")  # by the claude.ai id too
        assert s["session"]["session_id"] == "s" and s["session"]["url"] == "https://claude.ai/code/session_01X"
        assert [r["cause"] for r in s["rebuilds"]] == ["idle", "compaction"] and len(s["switches"]) == 1
        assert s["prs"][0]["number"] == 7
        pr = usage.detail(conn, 3, "pr", "7")
        assert pr["pr"]["sessions"] == 1 and pr["sessions"][0]["session_id"] == "s"
        items = {i["label"]: i for i in pr["items"]}
        assert items["/a.py"]["rides"] == 3 and "git status" not in items  # git status rode along on next-task
        assert usage.detail(conn, 3, "item", "command:git status")["prs"] == []
        assert usage.detail(conn, 3, "item", "file:/a.py")["prs"][0]["number"] == 7
        # a test red from minute 1 to 3 of that session: the read of /a.py at 1.1 came in then, and stayed for r4
        from greenlight.ingest import TestResult, record_run
        for minute, outcome in ((1, "fail"), (3, "pass")):
            record_run(conn, [TestResult("t::checkout", None, outcome, 5)], commit_sha="c1", source="ci",
                       started_at=T0 + timedelta(minutes=minute), session="session_01X")
        red = usage.detail(conn, 3, "test", "t::checkout")["items"]
        assert [(i["label"], i["adds"], i["carried_tokens"]) for i in red] == [("/a.py", 1, 1000)]
        cats = {c["category"] for c in pr["context"]["categories"]}
        assert {"test", "read", "image"} <= cats and "git" not in cats  # git ran on next-task, after the merge
        c = usage.detail(conn, 3, "category", "test")
        assert c["category"]["carried_tokens"] == 2000 and c["sessions"][0]["last_at"] and c["prs"][0]["number"] == 7
        w = usage.detail(conn, 3, "waste", "switches")
        assert w["rows"][0]["to_branch"] == "next-task" and w["rows"][0]["url"].endswith("session_01X")
        assert usage.detail(conn, 3, "waste", "idle")["rows"][0]["idle_seconds"] == 67 * 60
        assert usage.detail(conn, 3, "waste", "repeats")["rows"][0]["repeat_reads"] == 1
        for kind, key in (("session", "nope"), ("pr", "99"), ("waste", "nope"), ("nope", "x")):
            with pytest.raises(LookupError):
                usage.detail(conn, 3, kind, key)


def summed(rows):
    out = {}
    for r in rows:
        t = out.setdefault((r["label"], r["kind"]), {**r, "adds": 0, "tokens": 0, "rides": 0, "carried_tokens": 0})
        for f in ("adds", "tokens", "rides", "carried_tokens"):
            t[f] += r[f]
    return out


def test_each_item_and_how_many_requests_it_rode_along_with(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.md").write_text("x" * 2400)  # 1,000 tokens
    f = context.read_file(transcript(tmp_path), root="/")
    raw = context.item_rows([f], context.instruction_files(str(root)))
    rows = summed(raw)
    a = rows[("a.py", "file")]
    assert (a["adds"], a["tokens"], a["rides"], a["max_rides"]) == (3, 3000, 3, 2)  # read by r2 (2 more), r3 (1), r4 (0)
    assert rows[("npm test", "command")]["rides"] == 2 and rows[("cat", "command")]["kind"] == "command"
    assert rows[("npm test", "command")]["group"] == "test runs" and rows[("cat", "command")]["group"] == "file reads in a shell"
    assert rows[("shot.png", "image")]["tokens"] == 100
    assert rows[("github: get_pr", "mcp")]["adds"] == 1 and rows[("git status", "command")]["carried_tokens"] == 1000
    assert not any(k == "edit" or label.startswith("Edit") for label, k in rows)  # an edit's confirmation isn't followed
    claude = rows[("CLAUDE.md", "instructions")]
    # every request that read the cache carried it (r2-r4, r7-r9); the window starts and the rebuild wrote it
    assert (claude["adds"], claude["rides"], claude["max_rides"], claude["carried_tokens"]) == (3, 6, 3, 6000)


def test_an_items_re_reads_are_charged_to_the_branch_that_carried_them(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.md").write_text("x" * 2400)
    raw = context.item_rows([context.read_file(transcript(tmp_path), root="/")], context.instruction_files(str(root)))
    by = {(r["label"], r["branch"]): r for r in raw}
    fix, nxt = by[("CLAUDE.md", "fix")], by[("CLAUDE.md", "next-task")]
    assert (fix["adds"], fix["rides"]) == (3, 3) and (nxt["adds"], nxt["rides"]) == (0, 3)  # r1-r6 on fix, r7-r9 on the next
    assert fix["seg_start"] < nxt["seg_start"]
    assert ("git status", "fix") not in by and by[("git status", "next-task")]["rides"] == 1
    assert by[("a.py", "fix")]["rides"] == 3 and ("a.py", "next-task") not in by  # compacted away before the switch


def test_each_costly_entry_is_kept_with_when_it_came_in(tmp_path):
    adds = context.add_rows([context.read_file(transcript(tmp_path), root="/")])
    a = [r for r in adds if r["label"] == "a.py"]
    assert len(a) == 2 and all(r["at"] and r["branch"] == "fix" for r in a)  # the third read was never re-read
    assert sum(r["carried_tokens"] for r in a) == 3000 and adds == sorted(adds, key=lambda r: -r["carried_tokens"])


def test_labels_never_carry_a_commands_arguments():
    label = context.command_label
    assert label("cd app && npm run test:changed -- --dry") == "npm run test:changed"
    assert label("curl -s -H 'Authorization: Bearer abc' https://x.example/y") == "curl"
    assert label("GH_TOKEN=abc gh pr view 3") == "gh pr view"
    assert label("env -u A -u B .venv/bin/pytest -q tests/x.py") == "pytest"
    assert label("echo hunter2") == "echo" and label("export TOKEN=abc") == "export"
    assert context._rel("/home/u/r/src/a.ts", "/home/u/r") == "src/a.ts" and context._group("out/shots/a-1.png") == "out/shots/*.png"


def test_items_are_stored_summed_by_label_and_group_and_can_be_turned_off(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.md").write_text("x" * 2400)
    hook = {"transcript_path": str(transcript(tmp_path)), "session_id": "s", "cwd": str(root)}
    payload = usage.payload_from_hook(hook, {})
    assert any(i["kind"] == "instructions" for i in payload["items"])
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        usage.store(conn, payload)
        found = usage.items_summary(conn, "0")
        claude = next(i for i in found["items"] if i["label"] == "CLAUDE.md")
        assert claude["sessions"] == 1 and claude["avg_rides"] == 2 and claude["weighted"] > 0
        d = usage.detail(conn, 3, "item", "instructions:CLAUDE.md")
        assert d["item"]["label"] == "CLAUDE.md" and d["rows"][0]["session_id"] == "s"
        assert usage.detail(conn, 3, "session", "s")["items"]
        assert conn.execute("SELECT COUNT(*) FROM agent_context_adds").fetchone()[0] == len(payload["adds"])
        with pytest.raises(LookupError):
            usage.detail(conn, 3, "item", "file:nothing.py")
    (root / "greenlight.toml").write_text("[usage]\nitem_labels = false\n")
    assert "items" not in usage.payload_from_hook(hook, {})  # categories and counts only


def test_items_from_before_branch_runs_are_dropped_and_made_again(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE agent_context_items (session_id TEXT, label TEXT, kind TEXT, grp TEXT, first_at TEXT, "
                "branch TEXT, adds INTEGER, tokens INTEGER, rides INTEGER, max_rides INTEGER, carried_tokens INTEGER, "
                "PRIMARY KEY (session_id, label, kind))")
    old.execute("INSERT INTO agent_context_items VALUES ('s', 'a.py', 'file', '*.py', '2026-01-01', 'x', 1, 1, 1, 1, 1)")
    old.execute("PRAGMA user_version = 6")
    old.commit()
    old.close()
    with closing(connect(str(path))) as conn:  # the next hook run sends every live session's items again
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_context_items)")}
        assert "seg_start" in cols and conn.execute("SELECT COUNT(*) FROM agent_context_items").fetchone()[0] == 0
