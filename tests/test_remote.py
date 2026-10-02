"""Repo mode: a GitHub repo read through greenlight's own cache clone, over stdio and HTTP. A local bare
repo stands in for GitHub (GREENLIGHT_GIT_BASE), so nothing here touches the network."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from greenlight import remote, sync
from greenlight.db import connect
from greenlight.gitrepo import Repo
from tests.conftest import child_env
from tests.test_playtest import game, sh  # noqa: F401 - fixture

@pytest.fixture
def hosted(game, tmp_path, monkeypatch):  # noqa: F811
    """`game` pushed to a bare repo at <base>/acme/game.git, served over file:// with filters allowed,
    the way GitHub serves a partial clone."""
    repo, head = game
    base = tmp_path / "github"
    bare = base / "acme" / "game.git"
    bare.parent.mkdir(parents=True)
    sh(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    sh(bare, "config", "uploadpack.allowFilter", "true")
    sh(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    home = tmp_path / "glhome"
    for var, value in (("GREENLIGHT_GIT_BASE", base.as_uri()), ("GREENLIGHT_HOME", str(home))):
        monkeypatch.setenv(var, value)
    for var in ("GREENLIGHT_DB", "GREENLIGHT_CONFIG", "GREENLIGHT_REPO", "GH_TOKEN", "GITHUB_TOKEN", "GREENLIGHT_GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return repo, bare, home, head


def test_names_are_checked():
    assert remote.check("acme/game") == "acme/game"
    assert remote.check("https://github.com/acme/game.git") == "acme/game"
    for bad in ("acme", "acme/game/extra", "../etc/passwd", "acme/..", "acme game/x"):
        with pytest.raises(ValueError):
            remote.check(bad)


def test_cache_clone_reads_like_a_checkout(hosted):
    repo, bare, home, head = hosted
    cfg = remote.config_for("acme/game")
    clone = Repo(cfg.git_path)
    assert Path(cfg.git_path) == home / "repos" / "acme" / "game"
    assert clone.partial  # contents come on demand
    assert not any(p.name != ".git" for p in Path(cfg.git_path).iterdir())  # nothing checked out
    assert cfg.repo == "acme/game" and cfg.playtest_enabled and cfg.db == str(home / "acme-game.db")
    assert cfg.get("delivery", "deploy_releases") is True  # nothing committed, no notes or release branch
    with closing(connect(cfg.db)) as conn:
        out = sync.run(conn, cfg, {"playtest"})
        assert out["sources"]["playtest"]["records"] == 3  # both branches, through the partial clone
        assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 1  # known-failures.json on main


def test_committed_config_wins_and_fetch_picks_up_new_commits(hosted):
    repo, bare, home, head = hosted
    remote.config_for("acme/game")
    (repo / "greenlight.toml").write_text('db = "~/elsewhere.db"\n[delivery]\ndeploy_branch = "release"\n')
    sh(repo, "add", "greenlight.toml")
    sh(repo, "commit", "-qm", "config")
    sh(repo, "push", "-q", str(bare), "main")
    cfg = remote.config_for("acme/game")
    assert cfg.get("delivery", "deploy_branch") == "release" and not cfg.get("delivery", "deploy_releases")
    assert cfg.db == str(home / "acme-game.db")  # a committed db path names someone else's disk


def test_a_missing_repo_says_how_to_fix_it(hosted):
    with pytest.raises(RuntimeError, match="couldn't clone acme/nope.*token"):
        remote.config_for("acme/nope")


def test_refresher_runs_one_sync_at_a_time_and_waits_out_the_interval():
    gate, calls = threading.Event(), []
    r = remote.Refresher(lambda: (calls.append(1), gate.wait(5)), every=3600)
    r.kick()
    r.kick()  # one is already running
    assert not r.wait_ready(0.05)  # no data yet: callers wait for the first sync
    gate.set()
    assert r.wait_ready(5)
    r._thread.join(5)
    r.kick()  # inside the interval: nothing new
    assert len(calls) == 1
    r.every = 0
    r.kick()
    r._thread.join(5)
    assert len(calls) == 2
    stale = remote.Refresher(lambda: gate.wait(5), ready=True)  # data on disk from an earlier run
    stale.kick()
    assert stale.wait_ready(0)


def test_refresher_reports_a_failed_sync():
    bad = remote.Refresher(lambda: 1 / 0)
    bad.kick()
    assert bad.wait_ready(5) and "ZeroDivisionError" in bad.error
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        bad.now()


def _env(hosted, **extra) -> dict:
    repo, bare, home, head = hosted
    return child_env(GREENLIGHT_GIT_BASE=os.environ["GREENLIGHT_GIT_BASE"], GREENLIGHT_HOME=str(home), **extra)


def test_stdio_server_reads_the_repo_with_no_checkout(hosted, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    elsewhere = tmp_path / "desktop"
    elsewhere.mkdir()

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "greenlight", "mcp", "--repo", "acme/game"],
                                       env=_env(hosted), cwd=str(elsewhere))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            res = await s.call_tool("greenlight_overview", {})
            return names, json.loads(res.content[0].text)

    names, out = asyncio.run(go())
    assert "greenlight_overview" in names and not {"greenlight_gate_junit", "greenlight_playtest_gate"} & names
    assert out["repo"] == "acme/game" and out["source"] == "GitHub (acme/game)"
    assert out["flaky_tests"][0]["test_id"] == "smoke::the Lantern points at the foe it locked on"
    assert out["quarantined_tests"] == 1 and "playtest" in out["synced"]


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _status(url: str, headers: dict | None = None) -> int:
    req = urllib.request.Request(url, headers=headers or {}, data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_server_needs_the_token_in_the_path_or_a_header(hosted, tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "greenlight", "serve", "--repo", "acme/game", "--host", "0.0.0.0",
                             "--port", str(port)], env=_env(hosted, GREENLIGHT_MCP_TOKEN="t0ken-abc"), cwd=str(tmp_path),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        json_post = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        assert _status(base + "/mcp", json_post) == 401
        assert _status(base + "/wrong/mcp", json_post) == 401
        assert _status(base + "/mcp", {**json_post, "authorization": "Bearer nope"}) == 401

        async def call(url: str, headers: dict | None = None) -> list:
            async with create_mcp_http_client(headers=headers) as http, \
                    streamable_http_client(url, http_client=http) as (r, w), ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("greenlight_list_flaky", {})
                return json.loads(res.content[0].text)

        assert asyncio.run(call(base + "/t0ken-abc/mcp"))[0]["flip_shas"] == 1
        assert asyncio.run(call(base + "/mcp", {"Authorization": "Bearer t0ken-abc"}))[0]["flip_shas"] == 1
    finally:
        proc.terminate()
        proc.wait(10)
    assert b"t0ken-abc" not in proc.stderr.read().replace(b"/t0ken-abc/mcp", b"")  # only in the URL it prints
