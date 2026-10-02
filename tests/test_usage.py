"""Claude Code token usage: reading transcripts, storing per-minute totals, and charging them to pull requests
and to tests while they were red."""
import io
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from greenlight import cli, usage
from greenlight.db import connect, iso
from greenlight.ingest import TestResult as Result, record_run

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=1)


def at(minutes: float) -> str:
    return iso(T0 + timedelta(minutes=minutes))


def line(minutes: float, msg_id: str, out: int, branch: str = "fix-login", model: str = "claude-opus-5-5",
         block: str = "text", cache_read: int = 1000) -> str:
    return json.dumps({"type": "assistant", "timestamp": at(minutes).replace("+00:00", "Z"), "gitBranch": branch,
                       "sessionId": "s-local", "message": {"id": msg_id, "model": model, "content": [{"type": block}],
                       "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": cache_read,
                                 "cache_creation_input_tokens": 50}}})


@pytest.fixture
def transcript(tmp_path):
    main = tmp_path / "s-local.jsonl"
    main.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        line(0, "m1", 100, block="thinking"),
        line(0, "m1", 100, block="text"),  # the same response, next content block: counted once
        line(1, "m2", 40),
        line(1, "m3", 0, model="<synthetic>"),  # Claude Code's own placeholder, not an API call
        "not json",
        line(5, "m4", 60, branch="main"),
    ]) + "\n")
    sub = tmp_path / "s-local" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text(line(1.5, "m5", 7) + "\n")
    return main


def test_a_transcript_becomes_per_minute_counts(transcript):
    entries = usage.read_transcript(transcript)
    assert sorted(e["output_tokens"] for e in entries) == [7, 40, 60, 100]  # m1 once, subagent included
    rows = usage.minute_rows(entries)
    assert [(r["minute"], r["branch"], r["requests"], r["output_tokens"]) for r in rows] == [
        (at(0), "fix-login", 1, 100), (at(1), "fix-login", 2, 47), (at(5), "main", 1, 60)]
    assert rows[1]["cache_read_tokens"] == 2000 and rows[1]["cache_write_tokens"] == 100


def test_storing_a_session_again_replaces_it(transcript, tmp_path):
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        env = {"CLAUDE_CODE_SESSION_ID": "s-local", "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01ABC"}
        payload = usage.payload_from_hook({"transcript_path": str(transcript), "session_id": "s-local"}, env)
        assert payload["session"]["remote_session"] == "session_01ABC"
        assert usage.store(conn, payload) == {"session_id": "s-local", "minutes": 3}
        usage.store(conn, {"session": {"session_id": "s-local"}, "rows": payload["rows"][:1]})
        assert conn.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0] == 1
        assert conn.execute("SELECT remote_session FROM agent_sessions").fetchone()[0] == "session_01ABC"  # kept
        with pytest.raises(ValueError):
            usage.store(conn, {"session": {}, "rows": []})
        other = usage.payload_from_hook({"transcript_path": str(transcript), "session_id": "someone-else"}, env)
        assert other["session"]["remote_session"] is None  # this process's claude.ai id belongs to its own session


def row(minute: float, branch: str, out: int) -> dict:
    return {"minute": at(minute), "model": "m", "branch": branch, "requests": 1, "input_tokens": 1,
            "output_tokens": out, "cache_read_tokens": 10, "cache_write_tokens": 2}


def test_a_pr_gets_its_branch_until_it_closed_and_a_reused_name_goes_to_the_next(tmp_path):
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        conn.executemany("INSERT INTO pull_requests (number, title, state, head, created_at, merged_at) "
                         "VALUES (?,?,?,?,?,?)", [(1, "First", "merged", "fix", at(0), at(10)),
                                                  (2, "Second", "open", "fix", at(20), None)])
        usage.store(conn, {"session": {"session_id": "a"}, "rows": [row(1, "fix", 100), row(9, "fix", 5),
                                                                     row(15, "fix", 7), row(30, "fix", 50),
                                                                     row(31, "main", 1000)]})
        usage.store(conn, {"session": {"session_id": "b"}, "rows": [row(2, "fix", 1)]})
        s = usage.summary(conn, 3)
        prs = {p["number"]: p for p in s["by_pr"]}
        assert prs[1]["output_tokens"] == 106 and prs[1]["sessions"] == 2
        assert prs[2]["output_tokens"] == 57  # 15 came after #1 merged, before #2: the open one takes it
        assert s["unattributed"]["output_tokens"] == 1000  # main isn't a PR
        assert s["totals"]["output_tokens"] == 1163 and s["totals"]["sessions"] == 2
        assert usage.summary(conn, 3, pr=2)["pr"]["output_tokens"] == 57


def run(conn, minutes: float, session: str, outcomes: dict[str, str]) -> None:
    record_run(conn, [Result(t, None, o, 10) for t, o in outcomes.items()], commit_sha=f"c{minutes}",
               started_at=T0 + timedelta(minutes=minutes), session=session, source="playtest")


def test_a_test_costs_what_its_session_spent_while_it_was_red(tmp_path):
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        usage.store(conn, {"session": {"session_id": "s-local", "remote_session": "session_01ABC"},
                           "rows": [row(m, "fix", 10) for m in range(0, 60)]})
        run(conn, 5, "session_01ABC", {"a::login": "fail", "a::search": "fail", "a::ok": "pass"})
        run(conn, 20, "session_01ABC", {"a::login": "pass", "a::search": "fail"})
        run(conn, 30, "someone-elses-session", {"a::ok": "fail"})  # no usage for it: costs nothing
        tests = {t["test_id"]: t for t in usage.summary(conn, 3)["by_test"]}
        assert tests["a::login"]["output_tokens"] == 150 and tests["a::login"]["still_red"] == 0  # minutes 5..19
        assert tests["a::search"]["output_tokens"] == 550 and tests["a::search"]["still_red"] == 1  # red until the end
        assert "a::ok" not in tests


def test_the_hook_records_and_never_fails_the_session(transcript, tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "u.db")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-local", "transcript_path": str(transcript),
                                                             "cwd": str(tmp_path), "hook_event_name": "Stop"})))
    assert cli.main(["--db", db, "usage", "record", "--hook"]) == 0
    with closing(connect(db, readonly=True)) as conn:
        assert conn.execute("SELECT SUM(output_tokens) FROM agent_usage").fetchone()[0] == 207
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["--db", db, "usage", "record", "--hook"]) == 0  # a hook must never block Claude
    assert "greenlight usage:" in capsys.readouterr().err
    assert cli.main(["--db", db, "usage"]) == 0
    assert "1 sessions, 4 requests, 207 output" in capsys.readouterr().out


def test_greenlight_run_remembers_the_claude_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-local")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01ABC")
    assert usage.agent_session() == {"session_id": "s-local", "remote_session": "session_01ABC",
                                     "url": "https://claude.ai/code/session_01ABC"}
