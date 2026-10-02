"""`greenlight setup`: everything a repo needs to use greenlight from Claude or ChatGPT.

  greenlight.toml   written by `init` if the repo has none
  Claude Code       the MCP server registered at user scope, so every project gets it; the server
                    reads the greenlight.toml of whichever repo the session runs in
  --project         .mcp.json, .claude/settings.json and .codex/config.toml in the repo: every Claude
                    Code session on it (web included) and every Codex session starts greenlight through
                    uvx, with nothing installed by hand. Commit them and the whole team has it.
  --claude-desktop  Claude Desktop reads this repo straight from GitHub (no clone)
  --codex           the user-scope server in ~/.codex/config.toml (Codex CLI, IDE and ChatGPT desktop)
  CLAUDE.md         the rule that makes agents ask the gate before rerunning (printed, or written
                    with --agent-rules)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .gitrepo import Repo

SERVER_NAME = "greenlight"
# Tools that only read: approved in the repo's settings, so a session doesn't ask before each one. The ones
# that change something (quarantine, sweep with apply, issues, sync, gating a run) still ask.
READ_TOOLS = ("greenlight_overview", "greenlight_list_flaky", "greenlight_test_history", "greenlight_triage_run",
              "greenlight_pipelines", "greenlight_delivery", "greenlight_query", "greenlight_duration_regressions",
              "greenlight_suite_forecast", "greenlight_token_usage")
RULES_MARKER = "## Test failures (greenlight)"
AGENT_RULES = f"""{RULES_MARKER}
Record every test run with greenlight, passing or not (passes are the history that tells a flaky test
from a broken one), and on a failure follow its decision before rerunning anything. In a shell:
`greenlight run -- <test command>` (with GREENLIGHT_URL set, it sends the run to the shared server).
If the MCP server offers greenlight_gate_junit, calling it with the run's JUnit report does the same
(greenlight_playtest_gate for a playtest ledger).
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


# What a project's own config starts. uvx fetches greenlight (and Python if needed) on first use and
# caches it, so it works in a fresh cloud session where a SessionStart hook would run too late: Claude
# Code starts project MCP servers before the hook finishes.
UVX_COMMAND = ["uvx", "--from", "git+https://github.com/rathojohn/greenlight", "greenlight-mcp"]
# Runs after every Claude turn: reads the session's transcript and records its token counts (usage.py).
# `|| true`: if uvx itself fails (offline, say) it exits 2, and a Stop hook exiting 2 keeps Claude going.
USAGE_HOOK = " ".join(UVX_COMMAND[:3]) + " greenlight usage record --hook || true"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")  # -sig: Windows editors add a BOM
    except ValueError as e:
        raise ValueError(f"{path} isn't valid JSON ({e}); fix it and rerun") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} should hold a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _drop_toml_table(text: str, header: str) -> str:
    """text without the [header] table, up to the next table."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == header
        if not skipping:
            out.append(line)
    return "".join(out)


def project_files(target: Path, dry_run: bool = False, url: str | None = None, usage_hook: bool = True) -> list[str]:
    """Write (or merge into) the repo's own MCP config for Claude Code and Codex. Safe to run again.
    url: a greenlight server; sessions connect to it with $GREENLIGHT_TOKEN instead of starting their own.
    usage_hook: a Stop hook that records each Claude Code session's token counts."""
    done = []
    mcp_json = target / ".mcp.json"
    data = _read_json(mcp_json)
    if url:
        entry = {"type": "http", "url": f"{url.rstrip('/')}/mcp",
                 "headers": {"Authorization": "Bearer ${GREENLIGHT_TOKEN}"}}
    else:
        entry = {"type": "stdio", "command": UVX_COMMAND[0], "args": UVX_COMMAND[1:]}
    if data.get("mcpServers", {}).get(SERVER_NAME) == entry:
        done.append(".mcp.json: already set up")
    elif dry_run:
        done.append(".mcp.json: would add the greenlight server")
    else:
        data.setdefault("mcpServers", {})[SERVER_NAME] = entry
        _write_json(mcp_json, data)
        done.append(".mcp.json: added the greenlight server")

    settings = target / ".claude" / "settings.json"
    data = _read_json(settings)
    enabled = data.get("enabledMcpjsonServers") or []
    allow = (data.get("permissions") or {}).get("allow") or []
    missing = [f"mcp__{SERVER_NAME}__{t}" for t in READ_TOOLS if f"mcp__{SERVER_NAME}__{t}" not in allow]
    stop = (data.get("hooks") or {}).get("Stop") or []
    has_hook = any("greenlight usage record" in (h.get("command") or "") for g in stop for h in g.get("hooks") or [])
    want_hook = usage_hook and not has_hook
    if want_hook:
        if dry_run:
            done.append(".claude/settings.json: would add the hook that records token usage after each turn")
        else:
            data.setdefault("hooks", {})["Stop"] = [*stop, {"hooks": [{"type": "command", "command": USAGE_HOOK,
                                                                         "timeout": 60}]}]
            _write_json(settings, data)
            done.append(".claude/settings.json: added the hook that records token usage after each turn (counts "
                        "only, never prompts or code)")
    if SERVER_NAME in enabled and not missing:
        done.append(".claude/settings.json: greenlight and its read-only tools already approved")
    elif dry_run:
        done.append(".claude/settings.json: would approve greenlight and its read-only tools, so no session asks")
    else:
        if SERVER_NAME not in enabled:
            data["enabledMcpjsonServers"] = [*enabled, SERVER_NAME]
        if missing:
            data.setdefault("permissions", {})["allow"] = [*allow, *missing]
        _write_json(settings, data)
        done.append(".claude/settings.json: approved greenlight and its read-only tools, so no session asks "
                    "(the ones that change something still ask)")

    codex = target / ".codex" / "config.toml"
    text = codex.read_text(encoding="utf-8") if codex.is_file() else ""
    header = f"[mcp_servers.{SERVER_NAME}]"
    if url:
        block = f'{header}\nurl = {json.dumps(url.rstrip("/") + "/mcp")}\nbearer_token_env_var = "GREENLIGHT_TOKEN"\n'
    else:
        args = ", ".join(json.dumps(a) for a in UVX_COMMAND[1:])
        block = (f"{header}\ncommand = {json.dumps(UVX_COMMAND[0])}\nargs = [{args}]\n"
                 "startup_timeout_sec = 120   # the first start downloads it\n")
    if block in text:
        done.append(".codex/config.toml: already set up")
    elif dry_run:
        done.append(".codex/config.toml: would add the greenlight server")
    else:
        rest = _drop_toml_table(text, header).rstrip("\n")
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.write_text((rest + "\n\n" if rest else "") + block, encoding="utf-8")
        done.append(".codex/config.toml: set the greenlight server (Codex reads it in trusted projects)")
    return done


def claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / "claude_desktop_config.json"


def claude_desktop(repo: str, path: Path | None = None, dry_run: bool = False) -> str:
    """Claude Desktop starts the server with no project, so it reads the repo from GitHub. One entry
    per repo (greenlight-<name>), run by this install's interpreter: desktop apps don't get your
    shell's PATH, so `uvx` or `greenlight` might not be found there."""
    path = path or claude_desktop_path()
    name = f"{SERVER_NAME}-{repo.split('/')[-1]}"
    entry = {"command": sys.executable, "args": ["-m", "greenlight", "mcp", "--repo", repo]}
    data = _read_json(path)
    if data.get("mcpServers", {}).get(name) == entry:
        return f"{name} is already in {path}"
    if dry_run:
        return f"would add {name} to {path}"
    data.setdefault("mcpServers", {})[name] = entry
    _write_json(path, data)
    return f"added {name} to {path}. Quit and reopen Claude Desktop to load it."


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
