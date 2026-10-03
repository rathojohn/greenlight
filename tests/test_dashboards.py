"""Dashboards: panels of read-only SQL with variables, the period before and events, like Grafana's."""
import json
from contextlib import closing
from datetime import timedelta

import pytest

from greenlight import analysis, dashboards, web
from greenlight.db import connect, iso, utcnow
from greenlight.ingest import TestResult as Result, record_run

NOW = utcnow()


def ago(days: float = 0, hours: float = 0) -> str:
    return iso(NOW - timedelta(days=days, hours=hours))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "d.db")
    with closing(connect(path)) as conn:
        for day, branch, outcome in ((1, "main", "fail"), (2, "main", "pass"), (3, "fix", "fail"), (35, "main", "fail")):
            record_run(conn, [Result("t::login", None, outcome, 10)], commit_sha=f"c{day}", branch=branch,
                       started_at=NOW - timedelta(days=day), source="ci")
        conn.execute("INSERT INTO deployments VALUES ('d1', 'branch-push', 'production', 'c2', NULL, ?, 'success', 'v1', NULL)",
                     (ago(2),))
        conn.execute("INSERT INTO pull_requests (number, title, state, head, created_at, merged_at) VALUES "
                     "(4, 'Fix login', 'merged', 'fix', ?, ?)", (ago(4), ago(3)))
        conn.commit()
    yield path


FAILS = ("SELECT bucket(r.started_at) AS time, COUNT(*) AS failures FROM results x JOIN runs r USING (run_id) "
         "WHERE r.started_at >= :start AND r.started_at < :end AND (:branch IS NULL OR r.branch = :branch) "
         "AND x.outcome = 'fail' GROUP BY time")
SPEC = {"description": "Login health", "variables": [{"name": "branch", "sql": "SELECT DISTINCT branch FROM runs ORDER BY 1"}],
        "panels": [{"type": "stat", "title": "Failures", "sql": FAILS, "thresholds": {"warn": 1, "bad": 5}},
                   {"type": "row", "title": "Detail"},
                   {"type": "timeseries", "title": "Failures per day", "sql": FAILS, "compare": True},
                   {"type": "text", "text": "**Read me** first"}]}


def test_queries_only_read_and_stop_in_time(db):
    with closing(connect(db)) as conn:  # a connection that could write: the authorizer is what stops it
        for bad in ("WITH x AS (SELECT 1) DELETE FROM runs", "SELECT * FROM pragma_table_info('runs')",
                    "PRAGMA table_info(runs)", "SELECT 1; DELETE FROM runs"):
            with pytest.raises(ValueError):
                analysis.run_query(conn, bad)
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
        with pytest.raises(ValueError, match="longer than"):
            analysis.run_query(conn, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT MAX(i) FROM n",
                               seconds=0.05)
        assert analysis.run_query(conn, "SELECT cost(10, 2, 100, 4, 4)")["rows"] == [[38.0]]  # 10 + 10 + 10 + 5 + 3


def test_bucket_is_hourly_for_two_days_or_less():
    assert dashboards.bucket_fn(1)("2026-10-02T14:03:09+00:00") == "2026-10-02T14:00"
    assert dashboards.bucket_fn(7)("2026-10-02T14:03:09+00:00") == "2026-10-02"
    assert dashboards.bucket_fn(7)(None) is None


def test_a_dashboard_saves_only_when_every_query_runs(db):
    with closing(connect(db)) as conn:
        out = dashboards.save(conn, "Login", SPEC)
        assert out["id"] == "login" and [p["title"] for p in out["panels"]] == ["Failures", "Failures per day"]
        broken = {**SPEC, "panels": [*SPEC["panels"], {"type": "table", "title": "Oops", "sql": "SELECT nope FROM runs"}]}
        with pytest.raises(ValueError, match="Nothing saved.*Oops.*no such column"):
            dashboards.save(conn, "Login", broken)
        assert len(dashboards.get(conn, "login")["panels"]) == 4  # the good one stayed
        for bad, msg in (({"panels": [{"type": "pie", "title": "x", "sql": "SELECT 1"}]}, "type is one of"),
                         ({"panels": [{"type": "stat", "sql": "SELECT 1"}]}, "give it a title"),
                         ({"panels": [{"type": "stat", "title": "x", "sql": "SELECT 1", "width": 5}]}, "width"),
                         ({"variables": [{"name": "start", "values": ["a"]}]}, "not start"),
                         ({"variables": [{"name": "b", "values": ["a"]}, {"name": "b", "values": ["c"]}]}, "same name"),
                         ({"panels": [{"type": "stat", "title": "x", "sql": "SELECT :nope"}]}, "nope")):
            with pytest.raises(ValueError, match=msg):
                dashboards.save(conn, "Bad", bad)
        assert [d["id"] for d in dashboards.all_dashboards(conn)] == ["login"]
        assert dashboards.slug("What's trending") == "whats-trending"
        assert dashboards.delete(conn, "login") and not dashboards.all_dashboards(conn)


def test_a_dashboard_renders_with_its_variables_the_period_before_and_events(db):
    with closing(connect(db)) as conn:
        dashboards.save(conn, "Login", SPEC)
        d = dashboards.render(conn, "login", 30)
        assert d["variables"][0]["options"] == [["fix", "fix"], ["main", "main"]] and d["variables"][0]["value"] is None
        stat = d["panels"][0]
        assert sum(r[1] for r in stat["result"]["rows"]) == 2 and "previous" in stat  # stats compare by default
        ts = d["panels"][2]
        assert [r[0] for r in ts["previous"]["rows"]] == [(NOW - timedelta(days=5)).date().isoformat()]  # day 35, moved up 30
        assert [e["kind"] for e in d["events"]] == ["merge", "deploy"]
        main = dashboards.render(conn, "login", 30, {"branch": "main"})
        assert main["variables"][0]["value"] == "main" and sum(r[1] for r in main["panels"][0]["result"]["rows"]) == 1
        assert dashboards.render(conn, "login", 2)["panels"][0]["result"]["rows"][0][0].endswith(":00")  # hourly


def test_annotations_mark_a_change_on_every_time_series(db):
    notes = [{"name": "Trimmed CLAUDE.md", "sql": f"SELECT '{ago(10)}', NULL"},
             {"name": "Merges", "sql": "SELECT merged_at, 'Merged ' || title FROM pull_requests WHERE number = 4"},
             {"name": "Long ago", "sql": f"SELECT '{ago(40)}', 'Before the window'"}]
    with closing(connect(db)) as conn:
        for bad, msg in (([{"name": "x", "sql": "SELECT 1, 'not a time'"}], "Nothing saved.*annotation x.*first column is the time"),
                         ([{"name": "x"}], "annotation 1: send"), ([{"name": "x", "sql": "SELECT 'a'"}] * 11, "at most 10")):
            with pytest.raises(ValueError, match=msg):
                dashboards.save(conn, "Login", {**SPEC, "annotations": bad})
        dashboards.save(conn, "Login", {**SPEC, "annotations": notes})
        d = dashboards.render(conn, "login", 30)
        assert [(e["kind"], e.get("label")) for e in d["events"]] == [
            ("annotation", "Trimmed CLAUDE.md"), ("merge", "Merged #4 Fix login"), ("annotation", "Merged Fix login"),
            ("deploy", "Deployed v1 to production")]
        assert [a["name"] for a in d["annotations"]] == ["Trimmed CLAUDE.md", "Merges", "Long ago"]
        conn.execute("UPDATE pull_requests SET merged_at = 'soon'")  # data changed under a saved query
        d = dashboards.render(conn, "login", 30)
        assert "first column" in d["annotations"][1]["error"] and [e["kind"] for e in d["events"]] == ["annotation", "deploy"]
        assert web.GET_ROUTES["/api/dashboard"](conn, {"id": "login", "days": "30"})["annotations"][0]["sql"] == notes[0]["sql"]


def test_the_starter_dashboard_runs_on_an_empty_database(tmp_path):
    with closing(connect(str(tmp_path / "empty.db"))) as conn:
        out = dashboards.add_starter(conn)
        assert out["id"] == "whats-trending" and len(out["panels"]) == 11
        assert not [p for p in dashboards.render(conn, out["id"], 7)["panels"] if "error" in p]


def test_the_schema_tells_an_agent_every_table_and_how_panels_draw():
    s = dashboards.schema()
    tables = {t["table"]: t for t in s["tables"]}
    assert {"runs", "results", "agent_usage", "agent_commits", "dashboards"} <= set(tables)
    assert any(c.startswith("commit_sha TEXT: identity of the code tested") for c in tables["runs"]["columns"])
    assert "bucket(time)" in " ".join(s["conventions"]) and s["types"] == list(dashboards.TYPES)
    assert len(json.dumps(s)) < 16_000  # it lands in an agent's context


def test_the_page_saves_renders_and_queries_through_the_api(db):
    with closing(connect(db)) as conn:
        web.POST_ROUTES["/api/dashboards"](conn, {"title": "Login", **SPEC})
        d = web.GET_ROUTES["/api/dashboard"](conn, {"id": "login", "days": "30", "var-branch": "fix"})
        assert d["variables"][0]["value"] == "fix"
        assert web.GET_ROUTES["/api/dashboards"](conn, {})["dashboards"][0]["panels"] == 3  # the row isn't a panel
        q = web.GET_ROUTES["/api/sql"](conn, {"q": FAILS, "days": "30", "var-branch": "__all"})
        assert sum(r[1] for r in q["rows"]) == 2
        assert web.POST_ROUTES["/api/dashboards/delete"](conn, {"id": "login"}) == {"deleted": True}
        assert web.POST_ROUTES["/api/dashboards"](conn, {"starter": True})["id"] == "whats-trending"
