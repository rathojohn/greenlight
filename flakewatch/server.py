"""flakewatch MCP server (stdio). Register with:
    claude mcp add flakewatch -- python -m flakewatch.server
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from . import analysis, forecast
from .db import connect

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

mcp = _Server(
    "flakewatch_mcp",
    instructions=(
        "Flaky-test history for this project. After any test run, call flakewatch_triage_run before deciding "
        "to rerun anything. Only run a full regression when the decision is REAL_FAILURE. For RERUN_TARGETED, "
        "rerun only the listed rerun_tests."
    ),
)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

WindowDays = Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")]


def _run(fn: Callable[[sqlite3.Connection], Any], readonly: bool = True) -> str:
    """Open a connection per call, return compact JSON, and turn errors into actionable text."""
    try:
        with closing(connect(readonly=readonly)) as conn:
            return json.dumps(fn(conn), default=str, separators=(",", ":"))
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as e:
        return f"Error: {e}"
    except sqlite3.Error as e:
        return f"Error: SQLite: {e}. Check the SQL or table names (runs, results, quarantine)."


@mcp.tool(name="flakewatch_triage_run", annotations=READ)
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


@mcp.tool(name="flakewatch_list_flaky", annotations=READ)
def list_flaky(
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
    min_flips: Annotated[int, Field(ge=1, le=50, description="Minimum SHAs where the test both passed and failed.")] = 1,
    include_quarantined: bool = True,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> str:
    """Rank tests by flakiness. flip_shas = commits where the test both passed and failed;
    eligible_shas = commits where it ran 2+ times; flake_score = Wilson lower bound of flip rate."""
    return _run(lambda c: analysis.list_flaky(c, window_days, min_flips, include_quarantined, limit))


@mcp.tool(name="flakewatch_test_history", annotations=READ)
def test_history(
    test_id: Annotated[str, Field(min_length=1, description="Exact id 'classname::name' or a unique substring.")],
    limit: Annotated[int, Field(ge=1, le=500)] = 30,
) -> str:
    """Recent executions of one test (newest first) with 30-day flake stats and quarantine status."""
    return _run(lambda c: analysis.test_history(c, test_id, limit))


@mcp.tool(name="flakewatch_quarantine", annotations=WRITE)
def quarantine_test(
    test_id: Annotated[str, Field(min_length=1)],
    reason: Annotated[str, Field(min_length=3, max_length=300, description="Why. Shows up in triage output.")],
) -> str:
    """Quarantine a test. It keeps running and being recorded but no longer blocks triage."""
    return _run(lambda c: {"quarantined": analysis.quarantine(c, test_id, reason)}, readonly=False)


@mcp.tool(name="flakewatch_unquarantine", annotations=WRITE)
def unquarantine_test(test_id: Annotated[str, Field(min_length=1)]) -> str:
    """Remove a test from quarantine so its failures block again."""
    return _run(lambda c: {"removed": analysis.unquarantine(c, test_id)}, readonly=False)


@mcp.tool(name="flakewatch_sweep", annotations=WRITE)
def sweep(
    apply: Annotated[bool, Field(description="False = dry run. True = quarantine the listed tests.")] = False,
    window_days: WindowDays = analysis.DEFAULT_WINDOW_DAYS,
    min_flips: Annotated[int, Field(ge=1, le=50)] = analysis.DEFAULT_MIN_FLIPS,
    min_rate: Annotated[float, Field(ge=0, le=1, description="Minimum flip rate to auto-quarantine.")] = 0.05,
) -> str:
    """Find flaky tests that should be quarantined, plus quarantined tests that have been clean
    long enough to release. Releases are suggestions only; use flakewatch_unquarantine to act."""
    return _run(lambda c: analysis.sweep(c, window_days, min_flips, min_rate, apply=apply), readonly=not apply)


@mcp.tool(name="flakewatch_duration_regressions", annotations=READ)
def duration_regressions(
    holdout_days: Annotated[int, Field(ge=1, le=14, description="Recent days to check against the forecast.")] = 3,
    lookback_days: Annotated[int, Field(ge=21, le=365)] = 90,
    min_ratio: Annotated[float, Field(ge=0, le=5, description="Also require actual >= p50 * (1 + min_ratio).")] = 0.2,
) -> str:
    """Use Toto to forecast each test's daily median duration from history, then flag tests whose
    recent days came in above the p90 forecast. Slowdowns often show up before timeout flakes do."""
    return _run(lambda c: forecast.duration_regressions(c, holdout_days, lookback_days, min_ratio=min_ratio))


@mcp.tool(name="flakewatch_suite_forecast", annotations=READ)
def suite_forecast(
    metric: Literal["failure_rate", "suite_duration_ms", "runs", "reruns"] = "reruns",
    horizon_days: Annotated[int, Field(ge=1, le=30)] = 7,
    lookback_days: Annotated[int, Field(ge=21, le=365)] = 90,
) -> str:
    """Toto forecast of a suite-level daily metric (p10/p50/p90), the last 14 days of actuals, and
    whether the last 3 days broke above the forecast. 'reruns' tracks rerun churn over time."""
    return _run(lambda c: forecast.suite_forecast(c, metric, lookback_days, horizon_days))


@mcp.tool(name="flakewatch_query", annotations=READ)
def query(
    sql: Annotated[str, Field(min_length=6, description="Single SELECT. Tables: runs, results, quarantine.")],
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
) -> str:
    """Read-only SQL escape hatch for questions the other tools don't cover. Connection is opened
    read-only. results.outcome is pass|fail|error|skip; runs.started_at is ISO 8601 UTC."""
    return _run(lambda c: analysis.run_query(c, sql, limit))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
