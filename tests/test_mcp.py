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
