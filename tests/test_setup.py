import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from greenlight import cli, config, setup
from greenlight.gitrepo import Repo
from tests.conftest import junit_xml

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
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no claude CLI: setup prints the command instead
    assert cli.main(["setup", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "config: wrote" in out and setup.RULES_MARKER in out
    assert "claude mcp add --transport stdio --scope user greenlight -- " + sys.executable in out
    cfg = config.load(str(repo / "greenlight.toml"))
    assert cfg.get("run", "junit") is None and cfg.get("ci", "data_branch") == "greenlight-data"

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
    (repo / "greenlight.toml").write_text(f'db = "{db}"\n')
    git(repo, "add", "greenlight.toml")
    git(repo, "commit", "-qm", "config")
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "reports").mkdir()
    (repo / "reports/junit.xml").write_text(junit_xml([("app::ok", "pass", 0.1, None), ("app::flip", "fail", 0.1, "x")]))
    elsewhere = tmp_path / "dot-claude"
    elsewhere.mkdir()
    env = {"CLAUDE_PROJECT_DIR": str(repo), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}

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
