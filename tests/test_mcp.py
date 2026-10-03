import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import child_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("mcp")
    db = d / "demo.db"
    subprocess.run([sys.executable, "-m", "greenlight.demo", "--db", str(db), "--days", "21"],
                   check=True, capture_output=True, cwd=ROOT, env=child_env())
    return d, db


def test_tools_over_stdio(demo):
    """Start the server the way Claude Code and Codex do and call tools through the protocol."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    d, db = demo
    env = child_env(GREENLIGHT_DB=str(db))

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "greenlight.server"], env=env, cwd=str(d))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            out = {}
            for tool, args in (("greenlight_triage_run", {}), ("greenlight_pipelines", {}), ("greenlight_delivery", {}),
                               ("greenlight_issues", {}), ("greenlight_query", {"sql": "SELECT COUNT(*) AS n FROM pipelines"})):
                res = await s.call_tool(tool, args)
                out[tool] = res.content[0].text
            return names, out

    names, out = asyncio.run(go())
    assert {"greenlight_triage_run", "greenlight_playtest_gate", "greenlight_sync", "greenlight_pipelines",
            "greenlight_delivery", "greenlight_issues", "greenlight_query"} <= names
    assert json.loads(out["greenlight_triage_run"])["decision"] == "REAL_FAILURE"
    assert json.loads(out["greenlight_pipelines"])["totals"]["runs"] > 0
    assert json.loads(out["greenlight_delivery"])["dora"]["metrics"]["deployments"] > 0
    plan = json.loads(out["greenlight_issues"])
    assert plan["applied"] is False and isinstance(plan["actions"], list)
    assert json.loads(out["greenlight_query"])["rows"][0][0] > 0


def test_an_agent_builds_a_dashboard_over_mcp(demo):
    """What Claude does when asked for a dashboard: read the schema, try a query, save it, read it back."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    d, db = demo
    env = child_env(GREENLIGHT_DB=str(db))
    sql = ("SELECT bucket(r.started_at) AS time, x.test_id, COUNT(*) AS failures FROM results x JOIN runs r USING (run_id) "
           "WHERE r.started_at >= :start AND r.started_at < :end AND (:branch IS NULL OR r.branch = :branch) "
           "AND x.outcome = 'fail' GROUP BY time, x.test_id")

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "greenlight.server"], env=env, cwd=str(d))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            text = lambda res: res.content[0].text  # noqa: E731
            schema = json.loads(text(await s.call_tool("greenlight_schema", {})))
            tried = json.loads(text(await s.call_tool("greenlight_query", {"sql": sql.replace(":branch", "NULL"), "window_days": 7})))
            bad = text(await s.call_tool("greenlight_dashboard_save", {"title": "Bad", "panels": [
                {"type": "table", "title": "Nope", "sql": "SELECT nope FROM runs"}]}))
            saved = json.loads(text(await s.call_tool("greenlight_dashboard_save", {
                "title": "Flaky watch", "description": "Which tests fail, by day",
                "variables": [{"name": "branch", "label": "Branch", "sql": "SELECT DISTINCT branch FROM runs"}],
                "annotations": [{"name": "Newest run", "sql": "SELECT MAX(started_at), 'Newest run' FROM runs"}],
                "panels": [{"type": "stat", "title": "Failures", "sql": sql},
                           {"type": "timeseries", "title": "By test", "sql": sql, "display": "bars", "width": 12}]})))
            listed = json.loads(text(await s.call_tool("greenlight_dashboards", {})))
            one = json.loads(text(await s.call_tool("greenlight_dashboards", {"dashboard_id": "flaky-watch"})))
            return schema, tried, bad, saved, listed, one

    schema, tried, bad, saved, listed, one = asyncio.run(go())
    assert any(t["table"] == "agent_usage" for t in schema["tables"])
    assert tried["columns"] == ["time", "test_id", "failures"] and tried["rows"]
    assert "Nothing saved" in bad and "no such column" in bad
    assert saved["route"] == "#/dashboards/flaky-watch" and saved["panels"][0]["rows"] > 0
    assert listed["dashboards"][0]["title"] == "Flaky watch"
    assert one["variables"][0]["options"] and one["panels"][0]["type"] == "stat" and "previous" in one["panels"][0]
    assert one["annotations"] == [{"name": "Newest run", "sql": "SELECT MAX(started_at), 'Newest run' FROM runs"}]
