"""`greenlight setup`: everything a repo needs to use greenlight from Claude Code or Codex.

  greenlight.toml   written by `init` if the repo has none
  Claude Code       the MCP server registered at user scope, so every project gets it; the server
                    reads the greenlight.toml of whichever repo the session runs in
  Codex             the same server in ~/.codex/config.toml (with --codex)
  CLAUDE.md         the rule that makes agents ask the gate before rerunning (printed, or written
                    with --agent-rules)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .gitrepo import Repo

SERVER_NAME = "greenlight"
RULES_MARKER = "## Test failures (greenlight)"
AGENT_RULES = f"""{RULES_MARKER}
Record every test run with greenlight, passing or not (passes are the history that tells a flaky test
from a broken one), and on a failure follow its decision before rerunning anything. In a shell:
`greenlight run -- <test command>`. With the MCP server: call greenlight_gate_junit with the run's
JUnit report (or greenlight_playtest_gate for a playtest ledger).
- PASS: nothing that counts failed. Carry on.
- RERUN_TARGETED: only tests with a flake history failed. Rerun just those, once. No full regression.
- REAL_FAILURE: a test with no flake history failed. Investigate it; a full regression only after a fix.
Never quarantine or release a test, or apply issue changes, without saying why.
"""


def config_text(target: Path) -> str:
    repo = Repo(str(target))
    gh_repo = config.remote_repo(str(target)) if repo.ok() else None
    has_playtest = (target / "tools/playtest/runs").is_dir()
    has_release = bool(repo.ok() and (repo.resolve("refs/remotes/origin/release") or repo.resolve("refs/heads/release")))
    has_notes = (target / "docs/patch-notes").is_dir()
    name = (gh_repo or target.name).split("/")[-1]
    lines = [
        "# greenlight config. Every key is optional; `greenlight sync` reads this.",
        "# The GitHub token never goes here: see `greenlight auth`.",
        f'db = "~/.greenlight/{name}.db"',
        "",
        "[github]",
        f'repo = "{gh_repo}"' if gh_repo else '# repo = "owner/name"',
        "",
        "[playtest]",
        f"enabled = {'true' if has_playtest else 'false'}   # tools/playtest/runs records in git",
        "",
        "[actions]",
        "enabled = true",
        'junit_artifacts = "junit*"   # artifact names to ingest as test runs',
        "days = 30",
        "",
        "[delivery]",
        "# Pick the one that marks a release in this repo.",
        ('deploy_files = "docs/patch-notes/20??-??-??-*.md"   # each one added on main is a release (git only)'
         if has_notes else '# deploy_files = "CHANGELOG/*.md"   # each file added on the default branch is a release'),
        (f'{"# " if has_notes or not has_release else ""}deploy_branch = "release"   # every push to it is a deployment '
         "(needs a token)"),
        '# deploy_environment = "production"   # GitHub Deployments API',
        "# deploy_releases = true               # GitHub Releases",
        'incident_labels = ["incident", "hotfix", "bug"]',
        "",
        "[issues]",
        'flaky_label = "flaky-test"',
        'perf_label = "perf-regression"',
        'quarantine_label = "quarantined"   # label a flaky-test issue with it to quarantine the test',
        "",
        "[run]",
        '# junit = "reports/*.xml"   # the report `greenlight run` records (default: any JUnit XML the tests write)',
        "",
        "[otel]",
        '# endpoint = "http://localhost:4318"   # send everything to an OpenTelemetry backend on each sync',
        "",
    ]
    return "\n".join(lines)


def server_command() -> list[str]:
    """This install's own interpreter, so the agent runs the same greenlight you just set up."""
    return [sys.executable, "-m", "greenlight.server"]


def claude_mcp(dry_run: bool = False) -> str:
    """Register the MCP server with Claude Code at user scope. Returns what happened, in a line."""
    claude = shutil.which("claude")
    cmd = ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", SERVER_NAME, "--", *server_command()]
    if not claude:
        return "Claude Code CLI not found. Run this once it's installed:\n  " + " ".join(_quote(c) for c in cmd)
    if dry_run:
        return "would run: " + " ".join(_quote(c) for c in cmd)
    existing = subprocess.run([claude, "mcp", "get", SERVER_NAME], capture_output=True, text=True)
    if existing.returncode == 0:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True, text=True)
    out = subprocess.run([claude, *cmd[1:]], capture_output=True, text=True)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        return f"`claude mcp add` failed: {detail[-1] if detail else out.returncode}. Run it yourself:\n  " + \
            " ".join(_quote(c) for c in cmd)
    return ("updated" if existing.returncode == 0 else "added") + \
        " the greenlight MCP server in Claude Code (user scope: every project). Restart open sessions to load it."


def codex_snippet() -> str:
    cmd = server_command()
    args = ", ".join(f'"{a}"' for a in cmd[1:])
    return f'[mcp_servers.{SERVER_NAME}]\ncommand = "{_toml(cmd[0])}"\nargs = [{args}]\n'


def codex_mcp(path: Path | None = None) -> str:
    path = path or Path.home() / ".codex" / "config.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in text:
        return f"{path} already has [mcp_servers.{SERVER_NAME}]; left it alone"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if text and not text.endswith("\n\n") else "") + codex_snippet())
    return f"added [mcp_servers.{SERVER_NAME}] to {path}"


def write_agent_rules(target: Path, names: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")) -> list[str]:
    """Append the rule to the repo's agent files that exist (CLAUDE.md if none do). Skips files that have it."""
    files = [target / n for n in names if (target / n).is_file()] or [target / names[0]]
    done = []
    for f in files:
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        if RULES_MARKER in text:
            done.append(f"{f.name}: already has the greenlight rule")
            continue
        with f.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if text and not text.endswith("\n\n") else "") + AGENT_RULES)
        done.append(f"{f.name}: added the greenlight rule")
    return done


def _quote(s: str) -> str:
    return f'"{s}"' if any(c in s for c in " \t") else s


def _toml(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def env_note() -> str | None:
    if os.environ.get("GREENLIGHT_DB"):
        return "GREENLIGHT_DB is set in this shell; the MCP server only sees it if Claude Code's environment has it too."
    return None
