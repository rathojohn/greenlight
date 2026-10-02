import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from greenlight import cli, config, setup
from greenlight.gitrepo import Repo
from tests.conftest import child_env, hide_clis, junit_xml

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A git repo with one pytest file whose second test fails when FLIP=1."""
    for var in ("GREENLIGHT_CONFIG", "GREENLIGHT_DB", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "test_app.py").write_text("import os\n\ndef test_ok():\n    assert True\n\n"
                                      "def test_flip():\n    assert os.environ.get('FLIP') != '1'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def test_identity_is_the_commit_plus_uncommitted_edits(checkout):
    repo, sha = checkout
    r = Repo(str(repo))
    assert r.identity() == (sha, sha, "main")
    (repo / "reports").mkdir()
    (repo / "reports/junit.xml").write_text("<testsuite/>")
    assert r.identity(exclude={str(repo / "reports/junit.xml")})[0] == sha  # the report isn't code
    edited = r.identity()[0]
    assert edited.startswith(sha + "+")
    (repo / "test_app.py").write_text("edited\n")
    again = r.identity(exclude={str(repo / "reports/junit.xml")})[0]
    assert again.startswith(sha + "+") and again != edited
    assert Repo(str(repo / "reports")).identity(exclude={"reports/junit.xml"})[0] == again  # same from a subfolder


def test_run_records_and_gates_each_test_run(checkout, monkeypatch, tmp_path, capsys):
    repo, sha = checkout
    monkeypatch.chdir(repo)
    db = str(tmp_path / "run.db")
    pytest_cmd = ["--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    assert cli.main(["--db", db, "run", *pytest_cmd]) == 0
    assert "recorded run 1 (2 results)" in capsys.readouterr().out

    monkeypatch.setenv("FLIP", "1")  # same code, different result: flaky
    assert cli.main(["--db", db, "run", *pytest_cmd]) == 2
    out = capsys.readouterr().out
    assert "RERUN_TARGETED" in out and "rerun only: test_app::test_flip" in out

    (repo / "test_app.py").write_text((repo / "test_app.py").read_text() + "\ndef test_broken():\n    assert False\n")
    assert cli.main(["--db", db, "run", "--json", *pytest_cmd]) == 1  # an uncommitted edit is new code
    out = capsys.readouterr().out
    t = json.loads(out)  # pytest's own output goes to the terminal, not here
    assert t["decision"] == "REAL_FAILURE" and t["recorded"]["command_exit"] == 1
    assert t["blocking"] == ["test_app::test_broken"]
    assert t["rerun_tests"] == ["test_app::test_flip"]
    from contextlib import closing
    from greenlight.db import connect
    with closing(connect(db, readonly=True)) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT commit_sha, git_commit, branch FROM runs ORDER BY run_id")]
    assert [r[1] for r in rows] == [sha] * 3 and [r[2] for r in rows] == ["main"] * 3
    assert rows[0][0] == rows[1][0] == sha and rows[2][0].startswith(sha + "+")


def test_run_finds_reports_or_says_why_not(checkout, monkeypatch, tmp_path, capsys):
    repo, _ = checkout
    monkeypatch.chdir(repo)
    db = str(tmp_path / "run.db")
    assert cli.main(["--db", db, "run", "--", sys.executable, "-c", "pass"]) == 3
    assert "wrote no new JUnit XML" in capsys.readouterr().err
    assert cli.main(["--db", db, "run", "--", sys.executable, "-c", "raise SystemExit(4)"]) == 4

    write = ("import pathlib; pathlib.Path('out').mkdir(exist_ok=True); "
             f"pathlib.Path('out/results.xml').write_text({junit_xml([('a::b', 'pass', 0.1, None)])!r})")
    assert cli.main(["--db", db, "run", "--", sys.executable, "-c", write]) == 0  # found without --junit
    assert "recorded run 1 (1 results)" in capsys.readouterr().out
    assert cli.main(["--db", db, "run", "--junit", "out/*.xml", "--", sys.executable, "-c", write]) == 0
    assert cli.main(["--db", db, "run", "--junit", "out/*.xml", "--", sys.executable, "-c", "pass"]) == 3  # stale


def test_setup_writes_config_and_rules_once(checkout, monkeypatch, capsys):
    repo, _ = checkout
    hide_clis(monkeypatch)  # no claude CLI: setup prints the command instead
    assert cli.main(["setup", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "config: wrote" in out and setup.RULES_MARKER in out
    assert "claude mcp add --transport stdio --scope user greenlight -- " + sys.executable in out
    cfg = config.load(str(repo / "greenlight.toml"))
    assert cfg.get("run", "junit") is None and cfg.get("issues", "flaky_label") == "flaky-test"

    (repo / "AGENTS.md").write_text("# Agents\n")
    assert cli.main(["setup", str(repo), "--no-claude", "--agent-rules"]) == 0
    out = capsys.readouterr().out
    assert "already exists; left it alone" in out and "AGENTS.md: added the greenlight rule" in out
    assert setup.RULES_MARKER in (repo / "AGENTS.md").read_text() and not (repo / "CLAUDE.md").exists()
    assert cli.main(["setup", str(repo), "--no-claude"]) == 0
    assert setup.RULES_MARKER not in capsys.readouterr().out  # already there: not printed again
    assert setup.write_agent_rules(repo) == ["AGENTS.md: already has the greenlight rule"]


def test_codex_config_is_added_once(tmp_path):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "x"\n')
    assert "added" in setup.codex_mcp(path)
    assert "left it alone" in setup.codex_mcp(path)
    import tomllib
    data = tomllib.loads(path.read_text())
    assert data["model"] == "x"
    assert data["mcp_servers"]["greenlight"] == {"command": sys.executable, "args": ["-m", "greenlight.server"]}


def test_mcp_server_uses_the_claude_project(checkout, tmp_path):
    """A user-scoped server starts in ~/.claude; it must read the project's greenlight.toml and git state."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    repo, sha = checkout
    db = tmp_path / "from-config.db"
    (repo / "greenlight.toml").write_text(f'db = "{db.as_posix()}"\n')  # TOML strings treat \\ as an escape
    git(repo, "add", "greenlight.toml")
    git(repo, "commit", "-qm", "config")
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "reports").mkdir()
    (repo / "reports/junit.xml").write_text(junit_xml([("app::ok", "pass", 0.1, None), ("app::flip", "fail", 0.1, "x")]))
    elsewhere = tmp_path / "dot-claude"
    elsewhere.mkdir()
    env = child_env(CLAUDE_PROJECT_DIR=str(repo))

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "greenlight.server"], env=env,
                                       cwd=str(elsewhere))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("greenlight_gate_junit", {"paths": ["reports/*.xml"]})
            return json.loads(res.content[0].text)

    t = asyncio.run(go())
    assert t["decision"] == "REAL_FAILURE" and t["recorded"]["results"] == 2
    assert db.is_file()  # the project's config picked the DB
    from contextlib import closing
    from greenlight.db import connect
    with closing(connect(str(db), readonly=True)) as conn:
        assert tuple(conn.execute("SELECT commit_sha, branch, source FROM runs").fetchone()) == (sha, "main", "agent")


def test_project_files_merge_into_what_the_repo_has(tmp_path):
    import tomllib
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    (repo / ".claude" / "settings.json").write_text(json.dumps(hooks))
    (repo / ".mcp.json").write_text('\ufeff{"mcpServers": {"other": {"command": "x"}}}',
                                    encoding="utf-8")  # a BOM, as Notepad writes
    assert setup.project_files(repo, dry_run=True)[0] == ".mcp.json: would add the greenlight server"
    assert not (repo / ".codex").exists()
    first = setup.project_files(repo)
    assert all(("added" in line or "approved" in line or ": set" in line) for line in first), first
    servers = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["greenlight"] == {"type": "stdio", "command": "uvx", "args": setup.UVX_COMMAND[1:]}
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionStart"] == hooks["hooks"]["SessionStart"]  # the repo's own hooks stay
    assert settings["enabledMcpjsonServers"] == ["greenlight"]
    assert settings["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": setup.USAGE_HOOK, "timeout": 60}]}]
    assert settings["hooks"]["SubagentStart"] == [{"hooks": [{"type": "command", "command": setup.BRIEF_HOOK,
                                                             "timeout": 30}]}]
    assert "mcp__greenlight__greenlight_overview" in settings["permissions"]["allow"]
    assert "mcp__greenlight__greenlight_quarantine" not in settings["permissions"]["allow"]  # changes things: asks
    codex = tomllib.loads((repo / ".codex" / "config.toml").read_text())["mcp_servers"]["greenlight"]
    assert codex["command"] == "uvx" and codex["args"] == setup.UVX_COMMAND[1:] and codex["startup_timeout_sec"] == 120
    assert all("already" in line for line in setup.project_files(repo))
    hooks_now = json.loads((repo / ".claude" / "settings.json").read_text())["hooks"]
    assert len(hooks_now["Stop"]) == 1 and len(hooks_now["SubagentStart"]) == 1  # no second hook


def test_claude_desktop_gets_one_entry_per_repo(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    assert "added greenlight-game" in setup.claude_desktop("acme/game", path)
    assert "already" in setup.claude_desktop("acme/game", path)
    setup.claude_desktop("acme/tools", path)
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["greenlight-game"] == {"command": sys.executable, "args": ["-m", "greenlight", "mcp", "--repo", "acme/game"]}
    assert set(servers) == {"greenlight-game", "greenlight-tools"}
    path.write_text("{not json")
    with pytest.raises(ValueError, match="isn't valid JSON"):
        setup.claude_desktop("acme/game", path)


def test_setup_project_mode_writes_repo_files_not_user_config(checkout, monkeypatch, tmp_path, capsys):
    repo, _ = checkout
    desktop = tmp_path / "desktop.json"
    monkeypatch.setattr(setup, "claude_desktop_path", lambda: desktop)
    monkeypatch.setattr(setup, "claude_mcp", lambda dry_run=False: pytest.fail("project mode registered at user scope"))
    assert cli.main(["setup", str(repo), "--project", "--claude-desktop", "--repo", "acme/proj"]) == 0
    out = capsys.readouterr().out
    assert "project: .mcp.json: added" in out and "commit .mcp.json" in out
    assert "greenlight-proj" in json.loads(desktop.read_text())["mcpServers"]
    assert cli.main(["setup", str(repo), "--no-claude", "--claude-desktop"]) == 3  # no --repo, no GitHub remote
    assert "needs the GitHub repo" in capsys.readouterr().err


def test_project_files_can_point_at_a_server(tmp_path):
    import tomllib
    repo = tmp_path / "proj"
    (repo / ".codex").mkdir(parents=True)
    (repo / ".codex" / "config.toml").write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n')
    setup.project_files(repo)  # first the local, uvx-started server
    setup.project_files(repo, url="https://ci.example.com/")
    server = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["greenlight"]
    assert server == {"type": "http", "url": "https://ci.example.com/mcp",
                      "headers": {"Authorization": "Bearer ${GREENLIGHT_TOKEN}"}}
    codex = tomllib.loads((repo / ".codex" / "config.toml").read_text())
    assert codex["model"] == "x" and codex["mcp_servers"]["other"] == {"command": "o"}
    assert codex["mcp_servers"]["greenlight"] == {"url": "https://ci.example.com/mcp",
                                                  "bearer_token_env_var": "GREENLIGHT_TOKEN"}
    assert all("already" in line or "approved" in line
               for line in setup.project_files(repo, url="https://ci.example.com"))


def test_the_approved_tools_are_exactly_the_read_only_ones():
    """setup.py can't import the MCP server (the CLI is standard library only), so it keeps its own list."""
    import asyncio
    from greenlight import server
    tools = asyncio.run(server.mcp.list_tools())
    read_only = {t.name for t in tools if t.annotations and t.annotations.model_dump(by_alias=True).get("readOnlyHint")}
    assert set(setup.READ_TOOLS) == read_only
