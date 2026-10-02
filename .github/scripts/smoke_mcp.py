"""CI smoke test for the container: the HTTP MCP server answers with the token, refuses without it,
and reads the repo from GitHub. Usage: smoke_mcp.py <base url> <token> <owner/name>"""
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
