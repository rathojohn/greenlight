import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("mcp")
    db = d / "demo.db"
    subprocess.run([sys.executable, str(ROOT / "examples" / "seed_demo.py"), "--db", str(db), "--days", "21"],
                   check=True, capture_output=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    return d, db


def test_tools_over_stdio(demo):
    """Start the server the way Claude Code and Codex do and call tools through the protocol."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    d, db = demo
    env = {"FLAKEWATCH_DB": str(db), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "flakewatch.server"], env=env, cwd=str(d))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            out = {}
            for tool, args in (("flakewatch_triage_run", {}), ("flakewatch_pipelines", {}), ("flakewatch_delivery", {}),
                               ("flakewatch_issues", {}), ("flakewatch_query", {"sql": "SELECT COUNT(*) AS n FROM pipelines"})):
                res = await s.call_tool(tool, args)
                out[tool] = res.content[0].text
            return names, out

    names, out = asyncio.run(go())
    assert {"flakewatch_triage_run", "flakewatch_playtest_gate", "flakewatch_sync", "flakewatch_pipelines",
            "flakewatch_delivery", "flakewatch_issues", "flakewatch_query"} <= names
    assert json.loads(out["flakewatch_triage_run"])["decision"] == "REAL_FAILURE"
    assert json.loads(out["flakewatch_pipelines"])["totals"]["runs"] > 0
    assert json.loads(out["flakewatch_delivery"])["dora"]["metrics"]["deployments"] > 0
    plan = json.loads(out["flakewatch_issues"])
    assert plan["applied"] is False and isinstance(plan["actions"], list)
    assert json.loads(out["flakewatch_query"])["rows"][0][0] > 0
