"""greenlight MCP server.

  greenlight mcp                      stdio, for the project the agent works in (Claude Code, Codex)
  greenlight mcp --repo owner/name    stdio, reading the repo from GitHub: no clone (Claude Desktop)
  greenlight serve --repo owner/name  the hosted server (hosted.py): this over streamable HTTP, plus the
                                      dashboard and the ingest API, for every client at once

`greenlight setup` registers it with each client.
"""
from __future__ import annotations

import argparse
import glob
import hmac
import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from . import analysis, config, forecast, remote
from .db import connect, default_db_path, set_config_db

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

mcp = _Server(
    "greenlight_mcp",
    instructions=(
        "CI and test health for one GitHub repo: flaky tests, test run decisions, GitHub Actions pipelines, "
        "deployments and DORA metrics. Start with greenlight_overview. After a test run, record and judge it "
        "with greenlight_gate_junit (or greenlight_playtest_gate) before rerunning anything: PASS means carry on, "
        "RERUN_TARGETED means rerun only rerun_tests, REAL_FAILURE means investigate. Never quarantine a test or "
        "apply issue changes without telling the user why."
    ),
)

CHECKOUT_TOOLS = ("greenlight_gate_junit", "greenlight_playtest_gate")  # need the user's own checkout
FIRST_SYNC_WAIT = float(os.environ.get("GREENLIGHT_FIRST_SYNC_WAIT", "25"))


@dataclass
class _State:
    """Checkout mode: the project the agent works in (Claude Code says where in CLAUDE_PROJECT_DIR, since
    a user-scoped server starts in ~/.claude; Codex starts it in the project). Repo mode: a GitHub repo
    read through greenlight's own cache clone, kept fresh in the background."""
    project: Path = field(default_factory=Path.cwd)
    cfg: config.Config = field(default_factory=config.Config)
    repo: str | None = None
    refresher: remote.Refresher | None = None
    last_sync: dict[str, Any] | None = None


STATE = _State()


def configure(repo: str | None = None, project: str | Path | None = None) -> None:
    """Pick the mode. A repo whose checkout is the project is read from the checkout (local test runs
    included); any other repo is read from GitHub."""
    here = Path(project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    repo = repo or os.environ.get("GREENLIGHT_REPO") or None
    STATE.project, STATE.repo, STATE.refresher, STATE.last_sync = here, None, None, None
    if repo:
        repo = remote.check(repo)
        if (config.remote_repo(str(here)) or "").lower() != repo.lower():  # GitHub names ignore case
            STATE.repo = repo
            STATE.cfg = config.Config(sections={"github": {"repo": repo}}, db=remote.db_path(repo))
            set_config_db(STATE.cfg.db)
            STATE.refresher = remote.Refresher(_sync, ready=Path(default_db_path()).exists())
            return
    try:  # greenlight.toml in the project (or a parent) sets the DB and the repo
        STATE.cfg = config.load(start=here)
    except (ValueError, FileNotFoundError):
        STATE.cfg = config.Config(start=here)
    if repo:
        STATE.cfg.sections.setdefault("github", {})["repo"] = repo
    set_config_db(STATE.cfg.db)
    from . import sync
    from .gitrepo import Repo
    git = STATE.cfg.git_path
    if STATE.cfg.path is None and git and Repo(git).ok():  # no greenlight.toml, like a repo that ignores it
        checkout = Repo(git)
        remote.guess_delivery(STATE.cfg, checkout, checkout.default_branch())
    if sync.plan(STATE.cfg, SYNCED):  # a fresh cloud session starts with no DB: fill it on first use
        STATE.refresher = remote.Refresher(_sync, ready=Path(default_db_path()).exists())


SYNCED = {"playtest", "pulls", "issues", "actions", "deployments"}  # never otel from here


def _sync() -> None:
    from . import sync
    cfg = remote.config_for(STATE.repo) if STATE.repo else STATE.cfg
    with closing(connect()) as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # tool calls read while this writes
        STATE.last_sync = sync.run(conn, cfg, SYNCED)
    STATE.cfg = cfg


def _prune_tools() -> None:
    if STATE.repo:
        for name in CHECKOUT_TOOLS:
            try:
                mcp.remove_tool(name)
            except Exception:  # noqa: BLE001 - already removed (ToolError's home moves between mcp versions)
                pass


try:
    configure()
except ValueError:  # a bad $GREENLIGHT_REPO: main() and the CLI report it
    pass

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

WindowDays = Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")]


def not_ready() -> str | None:
    """Start a refresh if one is due, and wait out the very first one. A message when there's no data yet."""
    r = STATE.refresher
    if not r:
        return None
    r.kick()
    if not r.wait_ready(FIRST_SYNC_WAIT):
        return (f"greenlight is reading {STATE.repo or STATE.cfg.repo or 'this repo'} for the first time "
                "(run records, then pull requests, issues and Actions runs). Try again in a minute.")
    if r.error and not Path(default_db_path()).exists():
        return f"couldn't sync {STATE.repo or STATE.project}: {r.error}"
    return None


def _run(fn: Callable[[sqlite3.Connection], Any], readonly: bool = True) -> str:
    """Open a connection per call, return compact JSON, and turn errors into actionable text."""
    waiting = not_ready()
    if waiting:
        return f"Error: {waiting}"
    try:
        with closing(connect(readonly=readonly)) as conn:
            return json.dumps(fn(conn), default=str, separators=(",", ":"))
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as e:
        return f"Error: {e}"
    except sqlite3.Error as e:
        return f"Error: SQLite: {e}. Check the SQL or table names (see greenlight_query)."


@mcp.tool(name="greenlight_overview", annotations=READ)
def overview(window_days: WindowDays = 30) -> str:
    """Start here for "how is CI doing?": the latest test run's decision, the flakiest tests, quarantine,
    GitHub Actions health and DORA numbers in one call, plus which repo the data is from and when it was
    last synced. Each part says "unavailable" with the reason when there's no data for it yet."""
    from . import delivery

    def go(c: sqlite3.Connection) -> dict:
        out: dict[str, Any] = {"repo": STATE.cfg.repo, "source": f"GitHub ({STATE.repo})" if STATE.repo
                               else f"checkout at {STATE.project}", "synced": _synced(c)}

        def part(name: str, fn: Callable[[], Any]) -> None:
            try:
                out[name] = fn()
            except (LookupError, ValueError, KeyError, IndexError, sqlite3.Error) as e:
                out[name] = f"unavailable: {e}"

        def latest() -> dict:
            t = analysis.triage_run(c, None, None, window_days)
            return {k: t[k] for k in ("decision", "summary", "rerun_tests", "blocking")} | {"run": t.get("run")}

        def pipes() -> dict:
            p = delivery.pipelines_summary(c, window_days)
            return {"totals": p["totals"], "workflows": p["workflows"][:6], "flaky_jobs": p["flaky_jobs"][:5]}

        part("latest_run", latest)
        part("flaky_tests", lambda: [{k: r[k] for k in ("test_id", "flip_shas", "eligible_shas", "flake_score",
                                                          "classification", "quarantined")}
                                     for r in analysis.list_flaky(c, window_days, 1, True, 5)])
        part("quarantined_tests", lambda: c.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])
        part("pipelines", pipes)
        part("dora", lambda: delivery.dora(c, window_days, STATE.cfg.get("delivery", "incident_labels"))["metrics"])
        return out
    return _run(go)


def _synced(c: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {r[0]: r[1] for r in c.execute("SELECT source, synced_at FROM sync_state ORDER BY source")}
    if STATE.last_sync and STATE.last_sync.get("errors"):
        out["errors"] = STATE.last_sync["errors"]
    if STATE.refresher and STATE.refresher.error:
        out["last_refresh_failed"] = STATE.refresher.error
    return out


@mcp.tool(name="greenlight_triage_run", annotations=READ)
def triage_run(
    run_id: Annotated[int | None, Field(description="Run to triage. Omit to use the latest run.")] = None,
    commit_sha: Annotated[str | None, Field(description="Commit SHA or prefix; picks that SHA's latest run.")] = None,
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
) -> str:
    """Classify every failure in a run and return a decision.

    decision: PASS (nothing blocking), RERUN_TARGETED (only flaky failures; rerun just `rerun_tests`),
    or REAL_FAILURE (a stable or new test failed; see `blocking`). Each failure has a category:
    quarantined, known_flaky, suspect_flaky, new_test, or real_failure. `new_signature` = this flaky
    test failed with a message it has not produced before, which is worth a look."""
    return _run(lambda c: analysis.triage_run(c, run_id, commit_sha, window_days))


@mcp.tool(name="greenlight_gate_junit", annotations=WRITE)
def gate_junit(
    paths: Annotated[list[str], Field(min_length=1, description="JUnit XML files or globs, relative to the project.")],
    commit_sha: Annotated[str | None, Field(description="Default: the project's HEAD.")] = None,
    branch: Annotated[str | None, Field(description="Default: the project's current branch.")] = None,
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
) -> str:
    """Record a test run from its JUnit report and return the gate's decision in one call. Use it right
    after running tests: PASS (carry on), RERUN_TARGETED (rerun only `rerun_tests`, once) or
    REAL_FAILURE (investigate `blocking`; no full regression until it's fixed)."""
    from .ingest import ingest_checkout

    def go(c: sqlite3.Connection) -> dict:
        files: list[str] = []
        for p in paths:
            pattern = p if os.path.isabs(p) else str(STATE.project / p)
            files.extend(sorted(glob.glob(pattern, recursive=True)))
        if not files:
            raise ValueError(f"No JUnit files match {paths} in {STATE.project}. Did the runner write JUnit XML?")
        run_id, _, n = ingest_checkout(c, files, str(STATE.project), commit_sha, branch, source="agent")
        t = analysis.triage_run(c, run_id=run_id, window_days=window_days)
        t["recorded"] = {"run_id": run_id, "results": n, "files": len(files)}
        return t
    return _run(go, readonly=False)


@mcp.tool(name="greenlight_list_flaky", annotations=READ)
def list_flaky(
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
    min_flips: Annotated[int, Field(ge=1, le=50, description="Minimum SHAs where the test both passed and failed.")] = 1,
    include_quarantined: bool = True,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> str:
    """Rank tests by flakiness. flip_shas = commits where the test both passed and failed;
    eligible_shas = commits where it ran 2+ times; flake_score = Wilson lower bound of flip rate."""
    return _run(lambda c: analysis.list_flaky(c, window_days, min_flips, include_quarantined, limit))


@mcp.tool(name="greenlight_test_history", annotations=READ)
def test_history(
    test_id: Annotated[str, Field(min_length=1, description="Exact id 'classname::name' or a unique substring.")],
    limit: Annotated[int, Field(ge=1, le=500)] = 30,
) -> str:
    """Recent executions of one test (newest first) with 30-day flake stats and quarantine status."""
    return _run(lambda c: analysis.test_history(c, test_id, limit))


@mcp.tool(name="greenlight_quarantine", annotations=WRITE)
def quarantine_test(
    test_id: Annotated[str, Field(min_length=1)],
    reason: Annotated[str, Field(min_length=3, max_length=300, description="Why. Shows up in triage output.")],
) -> str:
    """Quarantine a test. It keeps running and being recorded but no longer blocks triage."""
    return _run(lambda c: {"quarantined": analysis.quarantine(c, test_id, reason)}, readonly=False)


@mcp.tool(name="greenlight_unquarantine", annotations=WRITE)
def unquarantine_test(test_id: Annotated[str, Field(min_length=1)]) -> str:
    """Remove a test from quarantine so its failures block again."""
    return _run(lambda c: {"removed": analysis.unquarantine(c, test_id)}, readonly=False)


@mcp.tool(name="greenlight_sweep", annotations=WRITE)
def sweep(
    apply: Annotated[bool, Field(description="False = dry run. True = quarantine the listed tests.")] = False,
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
    min_flips: Annotated[int, Field(ge=1, le=50)] = analysis.DEFAULT_MIN_FLIPS,
    min_rate: Annotated[float, Field(ge=0, le=1, description="Minimum flip rate to auto-quarantine.")] = 0.05,
) -> str:
    """Find flaky tests that should be quarantined, plus quarantined tests that have been clean
    long enough to release. Releases are suggestions only; use greenlight_unquarantine to act."""
    return _run(lambda c: analysis.sweep(c, window_days, min_flips, min_rate, apply=apply), readonly=not apply)


@mcp.tool(name="greenlight_duration_regressions", annotations=READ)
def duration_regressions(
    holdout_days: Annotated[int, Field(ge=1, le=14, description="Recent days to check against the forecast.")] = 3,
    lookback_days: Annotated[int, Field(ge=21, le=365)] = 90,
    min_ratio: Annotated[float, Field(ge=0, le=5, description="Also require actual >= p50 * (1 + min_ratio).")] = 0.2,
) -> str:
    """Use Toto to forecast each test's daily median duration from history, then flag tests whose
    recent days came in above the p90 forecast. Slowdowns often show up before timeout flakes do."""
    return _run(lambda c: forecast.duration_regressions(c, holdout_days, lookback_days, min_ratio=min_ratio))


@mcp.tool(name="greenlight_suite_forecast", annotations=READ)
def suite_forecast(
    metric: Literal["failure_rate", "suite_duration_ms", "runs", "reruns"] = "reruns",
    horizon_days: Annotated[int, Field(ge=1, le=30)] = 7,
    lookback_days: Annotated[int, Field(ge=21, le=365)] = 90,
) -> str:
    """Toto forecast of a suite-level daily metric (p10/p50/p90), the last 14 days of actuals, and
    whether the last 3 days broke above the forecast. 'reruns' tracks rerun churn over time."""
    return _run(lambda c: forecast.suite_forecast(c, metric, lookback_days, horizon_days))


@mcp.tool(name="greenlight_query", annotations=READ)
def query(
    sql: Annotated[str, Field(min_length=6, description="Single SELECT. Tables: runs, results, quarantine, metrics, "
                                                        "pipelines, jobs, steps, deployments, deploy_commits, "
                                                        "pull_requests, issues, sync_state.")],
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
) -> str:
    """Read-only SQL escape hatch for questions the other tools don't cover. Connection is opened
    read-only. results.outcome is pass|fail|error|skip and results.flags may hold inferred, slower or known;
    timestamps are ISO 8601 UTC; issues.labels is a JSON array; issues.managed_key marks the issues greenlight manages."""
    return _run(lambda c: analysis.run_query(c, sql, limit))


@mcp.tool(name="greenlight_playtest_gate", annotations=WRITE)
def playtest_gate(
    repo_path: Annotated[str | None, Field(description="The game's clone. Default: [git] path or the server's folder.")] = None,
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
) -> str:
    """For a playtest ledger (tools/playtest/runs records in git, like survive-project): read every record on
    every branch, then triage the run just made in this checkout. Returns the same decision as
    greenlight_triage_run plus rerun_command, the exact command that reruns only the flaky suites."""
    from . import playtest

    def go(c: sqlite3.Connection) -> dict:
        repo = repo_path or STATE.cfg.git_path or str(STATE.project)
        playtest.sync(c, repo)
        run_id = playtest.latest_local_run(c, repo)
        if run_id is None:
            raise LookupError("No playtest record in this checkout yet. Run `npm run test:changed` first.")
        t = analysis.triage_run(c, run_id=run_id, window_days=window_days)
        t["rerun_command"] = playtest.rerun_command(t["rerun_tests"])
        return t
    return _run(go, readonly=False)


@mcp.tool(name="greenlight_sync", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                               idempotentHint=True, openWorldHint=True))
def sync_sources(
    only: Annotated[list[Literal["playtest", "pulls", "issues", "actions", "deployments"]] | None,
                    Field(description="Sources to sync. Default: everything greenlight.toml configures.")] = None,
) -> str:
    """Pull git and GitHub data into greenlight's DB: playtest records, pull requests, issues, GitHub Actions
    runs and deployments. Reads GitHub only; writes only greenlight's own DB."""
    from . import sync
    if STATE.refresher and not only:
        try:
            STATE.refresher.now()
        except RuntimeError as e:
            return f"Error: {e}"
        return json.dumps(STATE.last_sync, default=str, separators=(",", ":"))
    return _run(lambda c: sync.run(c, STATE.cfg, set(only) if only else None), readonly=False)


@mcp.tool(name="greenlight_pipelines", annotations=READ)
def pipelines(window_days: WindowDays = 30) -> str:
    """GitHub Actions health: per-workflow runs, success rate, p50/p95 duration, queue time and reruns;
    flaky jobs (failed and passed on the same commit); slowest jobs; recent runs."""
    from . import delivery

    def go(c: sqlite3.Connection) -> dict:
        out = delivery.pipelines_summary(c, window_days)
        out["recent"] = out["recent"][:15]
        return out
    return _run(go)


@mcp.tool(name="greenlight_delivery", annotations=READ)
def delivery_metrics(
    window_days: WindowDays = 30,
    environment: Annotated[str | None, Field(description="Deployment environment. Default: the busiest one.")] = None,
) -> str:
    """DORA metrics (deployment frequency, lead time for changes, change failure rate, time to restore) with
    recent deployments, plus pull request flow and issue backlog."""
    from . import delivery

    def go(c: sqlite3.Connection) -> dict:
        out = delivery.delivery_view(c, window_days, STATE.cfg.get("delivery", "incident_labels"), environment)
        out["dora"]["deployments"] = out["dora"]["deployments"][:15]
        for k in ("days", "deploys_per_day", "lead_time_per_day"):
            out["dora"].pop(k, None)
        for k in ("days", "merged_per_day"):
            out["prs"].pop(k, None)
        for k in ("days", "opened_per_day", "closed_per_day", "open_per_day"):
            out["issues"].pop(k, None)
        return out
    return _run(go)


@mcp.tool(name="greenlight_issues", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                                 idempotentHint=True, openWorldHint=True))
def manage_issues(
    apply: Annotated[bool, Field(description="False = plan only. True = create, update and reopen the issues on GitHub.")] = False,
) -> str:
    """One GitHub issue per flaky test and per perf regression. Plans by default: what would be created,
    refreshed, reopened, linked to an existing issue, or get a 'clean long enough to close' comment.
    Only call with apply=true after the user agrees."""
    from . import ghsync, github, issues

    def go(c: sqlite3.Connection) -> Any:
        cfg = STATE.cfg
        gh = github.client(cfg.repo, cfg.api_url, require_token=apply) if (cfg.repo or apply) else None
        if gh:
            try:
                ghsync.sync_issues(c, gh, cfg.get("issues", "quarantine_label"))
            except github.GitHubError:
                if apply:
                    raise
        opts = {k: cfg.get("issues", k) for k in ("flaky_label", "perf_label", "quarantine_label", "min_flips",
                                                   "window_days", "healed_runs", "healed_days")}
        actions = issues.plan(c, opts)
        if not apply:
            return {"applied": False, "actions": json.loads(issues.as_json(actions))}
        return {"applied": True, "results": issues.apply(c, gh, actions)}
    return _run(go, readonly=False)


def main(argv: list[str] | None = None) -> None:
    """stdio entry point (greenlight-mcp, python -m greenlight.server)."""
    p = argparse.ArgumentParser(prog="greenlight-mcp", description="greenlight MCP server over stdio.")
    p.add_argument("--repo", help="owner/name: read it from GitHub, no clone needed (default: $GREENLIGHT_REPO)")
    p.add_argument("--project", help="the checkout to work in (default: $CLAUDE_PROJECT_DIR, else here)")
    a = p.parse_args(argv)
    try:
        configure(a.repo, a.project)
    except ValueError as e:
        sys.exit(f"greenlight-mcp: {e}")
    run_stdio()


def run_stdio() -> None:
    _prune_tools()
    if STATE.refresher:
        STATE.refresher.kick()  # start reading the repo now, not on the first question
    mcp.run()


if __name__ == "__main__":
    main()
