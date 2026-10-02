"""CI smoke test for the container: the server answers MCP and the dashboard API with the token, refuses
without it, and reads the repo from GitHub. Usage: smoke_mcp.py <base url> <token> <owner/name>"""
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

base, token, repo = sys.argv[1:4]


def status(url: str) -> int:
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


for _ in range(60):
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=2):
            break
    except OSError:
        time.sleep(1)
assert status(base + "/mcp") == 401, "a request without the token got in"
dash = urllib.request.Request(base + "/api/runs", headers={"Authorization": f"Bearer {token}"})
for attempt in range(12):  # 503 while the first sync is still reading the repo
    try:
        with urllib.request.urlopen(dash, timeout=30) as r:  # the dashboard API on the same port
            assert r.status == 200 and "runs" in json.loads(r.read()), "dashboard API"
        break
    except urllib.error.HTTPError as e:
        if e.code != 503 or attempt == 11:
            raise
        time.sleep(10)


async def overview() -> str:
    async with streamable_http_client(f"{base}/{token}/mcp") as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return (await s.call_tool("greenlight_overview", {})).content[0].text


for _ in range(12):  # the first sync can outlast one call's wait
    text = asyncio.run(overview())
    if not text.startswith("Error: greenlight is reading"):
        break
    time.sleep(10)
out = json.loads(text)
assert out["repo"] == repo and out["source"] == f"GitHub ({repo})", out
assert out["synced"], out
print(json.dumps({k: out[k] for k in ("repo", "source", "synced", "latest_run")}, indent=2, default=str)[:3000])
