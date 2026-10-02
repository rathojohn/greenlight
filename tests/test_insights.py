"""What to do next and the brief an agent starts with: failures on the default branch, flaky tests, what Claude
spent on them, and how the brief reaches a session (MCP instructions) and a subagent (the SubagentStart hook)."""
import io
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

from greenlight import cli, insights, server, usage
from greenlight.db import connect, iso
from greenlight.ingest import TestResult as Result, record_run

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=2)


def run(conn, hours: float, sha: str, outcomes: dict, branch: str = "main", session: str | None = None) -> None:
    record_run(conn, [Result(t, None, o, 10) for t, o in outcomes.items()], commit_sha=sha, branch=branch,
               started_at=T0 + timedelta(hours=hours), session=session, source="ci")


def history(path):
    conn = connect(str(path))
    run(conn, 0, "c1", {"a::login": "pass", "a::search": "pass", "a::export": "fail"})
    run(conn, 1, "c2", {"a::login": "fail", "a::search": "pass", "a::export": "fail"})
    run(conn, 2, "c3", {"a::login": "fail", "a::search": "fail", "a::export": "fail"})
    run(conn, 3, "c3", {"a::search": "pass"})  # search passed and failed on c3: flaky
    conn.execute("INSERT INTO quarantine (test_id, reason, added_at) VALUES ('a::export', 'broken', ?)", (iso(T0),))
    conn.commit()
    return conn


def test_red_on_the_default_branch_since_the_streak_began(tmp_path):
    with closing(history(tmp_path / "g.db")) as conn:
        [red] = insights.red_on_default(conn, 30)  # export is quarantined, search passed last
        assert red["test_id"] == "a::login" and red["runs"] == 2 and red["since_sha"] == "c2" and red["passed_before"]
        found = insights.insights(conn, 30)
        assert found[0]["id"] == "red:a::login" and found[0]["severity"] == "high"


def test_flaky_tests_claude_spent_tokens_on_rank_by_cost(tmp_path):
    with closing(history(tmp_path / "g.db")) as conn:
        run(conn, 4, "c4", {"a::search": "fail"}, branch="fix", session="s1")
        run(conn, 5, "c4", {"a::search": "pass"}, branch="fix", session="s1")
        usage.store(conn, {"session": {"session_id": "s1"}, "rows": [
            {"minute": iso(T0 + timedelta(hours=4, minutes=m)), "model": "m", "branch": "fix", "requests": 1,
             "output_tokens": 100, "cache_read_tokens": 10_000} for m in range(0, 60)]})
        found = {i["id"]: i for i in insights.insights(conn, 30)}
        cost = found["flaky-cost:a::search"]
        assert cost["stake"] == 60 * (500 + 1000) and "flaky search" in cost["title"]


def test_the_brief_says_what_to_leave_alone(tmp_path):
    with closing(history(tmp_path / "g.db")) as conn:
        text = insights.brief(conn, 30, "acme/game")
    assert text.startswith("greenlight (acme/game)")
    assert "not caused by your branch: login (since c2)" in text
    assert "Flaky, passed and failed on the same code: search (1/1 commits)" in text
    assert "1 quarantined test: the gate ignores its failures" in text
    assert "export" not in text.split("quarantined")[0]  # quarantined, so not listed as failing
    with closing(connect(str(tmp_path / "empty.db"))) as conn:
        assert insights.brief(conn, 30) == ""  # nothing to say: nothing said


def test_the_subagent_hook_answers_with_context_and_never_fails(tmp_path, monkeypatch, capsys):
    history(tmp_path / "g.db").close()
    db = str(tmp_path / "g.db")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SubagentStart",
                                                             "agent_type": "general-purpose"})))
    assert cli.main(["--db", db, "brief", "--hook"]) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "SubagentStart" and "login" in out["additionalContext"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SubagentStart", "agent_type": "Explore"})))
    assert cli.main(["--db", db, "brief", "--hook"]) == 0 and capsys.readouterr().out == ""  # searching needs no brief
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["--db", db, "brief", "--hook"]) == 0
    assert "greenlight brief:" in capsys.readouterr().err
    assert cli.main(["--db", db, "insights"]) == 0
    assert "[high] login fails on the default branch" in capsys.readouterr().out


def test_a_connecting_session_gets_the_brief_in_the_server_instructions(tmp_path, monkeypatch):
    history(tmp_path / "g.db").close()
    monkeypatch.setenv("GREENLIGHT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(server.STATE, "refresher", None)
    monkeypatch.setattr(server, "BRIEF", server._Brief())
    server.BRIEF.refresh()
    low = getattr(server.mcp, "_lowlevel_server", None) or server.mcp._mcp_server
    text = low.create_initialization_options().instructions
    assert text.startswith(server.INSTRUCTIONS) and "Failing on the default branch" in text
    assert server.mcp.instructions == text  # the same for a client that asks for it directly


def test_a_stdio_server_has_the_brief_for_its_first_and_only_connect(tmp_path, monkeypatch):
    history(tmp_path / "g.db").close()
    monkeypatch.setenv("GREENLIGHT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(server.STATE, "refresher", None)
    monkeypatch.setattr(server, "BRIEF", server._Brief())  # never computed, like a server that just started
    assert "login" in server.mcp.instructions


def test_what_rode_along_longest_becomes_advice():
    rows = [{"label": "CLAUDE.md", "kind": "instructions", "adds": 10, "tokens": 210_000, "weighted": 50_000,
             "avg_rides": 90, "max_rides": 300},
            {"label": "task list reminders", "kind": "reminder", "adds": 40, "tokens": 90_000, "weighted": 40_000,
             "avg_rides": 80, "max_rides": 200}]
    groups = [{"grp": "out/shots/*.png", "kind": "image", "labels": 34, "adds": 34, "weighted": 30_000, "avg_rides": 85}]
    found = {i["id"]: i for i in insights._item_insights({"items": rows, "groups": groups}, 1_000_000)}
    assert found["item:instructions:CLAUDE.md"]["title"] == "CLAUDE.md is about 21k tokens and rides along with every request"
    assert found["group:image:out/shots/*.png"]["title"] == "34 images from out/shots/*.png stayed in context for 85 requests on average"
    assert "task list" in found["item:reminder:task list reminders"]["agent"]
