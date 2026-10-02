"""flakewatch CLI.

  flakewatch ingest --sha $SHA reports/*.xml   record a run
  flakewatch gate --sha $SHA                   exit 0 PASS, 2 RERUN_TARGETED, 1 REAL_FAILURE
  flakewatch flaky                             ranked flaky tests
  flakewatch sweep [--apply]                   quarantine candidates / release candidates
  flakewatch trends                            Toto duration regressions + rerun forecast
  flakewatch ui                                dashboard at http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from contextlib import closing
from datetime import datetime

from . import analysis
from .db import connect
from .ingest import ingest_files


def _expand(patterns: list[str]) -> list[str]:
    """Expand globs ourselves so `reports/*.xml` works in PowerShell and cmd too."""
    files: list[str] = []
    for p in patterns:
        files.extend(sorted(glob.glob(p, recursive=True)) or [p])
    return files


def cmd_ingest(a: argparse.Namespace) -> int:
    started = datetime.fromisoformat(a.started_at) if a.started_at else None
    with closing(connect(a.db)) as conn:
        run_id, created, n = ingest_files(
            conn, _expand(a.files), commit_sha=a.sha, branch=a.branch, attempt=a.attempt,
            source=a.source, external_id=a.external_id, started_at=started)
    print(f"{'recorded' if created else 'already recorded'} run {run_id}: {n} results")
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    with closing(connect(a.db, readonly=True)) as conn:
        t = analysis.triage_run(conn, a.run_id, a.sha, a.window_days)
    if a.json:
        print(json.dumps(t, indent=2, default=str))
    else:
        print(f"{t['decision']}: {t['summary']}")
        for f in t["failures"]:
            extra = f" (flipped on {f['flip_shas']}/{f['eligible_shas']} SHAs)" if f["flip_shas"] else ""
            print(f"  [{f['category']}] {f['test_id']}{extra}")
        if t["rerun_tests"]:
            print("rerun only:", " ".join(t["rerun_tests"]))
    return t["exit_code"]


def cmd_flaky(a: argparse.Namespace) -> int:
    with closing(connect(a.db, readonly=True)) as conn:
        rows = analysis.list_flaky(conn, a.window_days, a.min_flips, True, a.limit)
    if not rows:
        print("No flips in window. Flakes only show up once a SHA has been run more than once.")
    for r in rows:
        q = " [quarantined]" if r["quarantined"] else ""
        print(f"{r['flake_score']:.3f}  {r['flip_shas']:>3}/{r['eligible_shas']:<3} {r['classification']:<8} {r['test_id']}{q}")
    return 0


def cmd_sweep(a: argparse.Namespace) -> int:
    with closing(connect(a.db, readonly=not a.apply)) as conn:
        print(json.dumps(analysis.sweep(conn, a.window_days, apply=a.apply), indent=2))
    return 0


def cmd_trends(a: argparse.Namespace) -> int:
    from . import forecast  # torch import is slow; only pay for it here
    with closing(connect(a.db, readonly=True)) as conn:
        out = {"duration_regressions": forecast.duration_regressions(conn, a.holdout_days, a.lookback_days)}
        try:
            out["reruns"] = forecast.suite_forecast(conn, "reruns", a.lookback_days)
        except ValueError as e:
            out["reruns"] = str(e)
    print(json.dumps(out, indent=2))
    return 0


def cmd_ui(a: argparse.Namespace) -> int:
    from . import web
    if a.export:
        n = web.export_snapshot(a.db, a.export, a.days, with_forecasts=not a.no_forecasts)
        print(f"wrote {a.export}: {n['responses']} views ({n['skipped']} could not be built and show an explanation)")
        return 0
    web.serve(a.db, a.port, open_browser=not a.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flakewatch")
    p.add_argument("--db", help="SQLite path (default: $FLAKEWATCH_DB or ~/.flakewatch/flakewatch.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="record a run from JUnit XML")
    s.add_argument("files", nargs="+", help="JUnit XML files or globs")
    s.add_argument("--sha", required=True)
    s.add_argument("--branch")
    s.add_argument("--attempt", type=int, help="default: 1 + prior runs on this SHA")
    s.add_argument("--source", default="local", help="ci, local, codex, claude-code...")
    s.add_argument("--external-id", help="CI run id; re-ingesting the same id is a no-op")
    s.add_argument("--started-at", help="ISO 8601; default now")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("gate", help="triage a run; exit 0 pass, 2 rerun flaky only, 1 real failure")
    s.add_argument("--sha")
    s.add_argument("--run-id", type=int)
    s.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("flaky", help="rank flaky tests")
    s.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    s.add_argument("--min-flips", type=int, default=1)
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_flaky)

    s = sub.add_parser("sweep", help="quarantine and release candidates")
    s.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("trends", help="Toto: duration regressions and rerun forecast")
    s.add_argument("--holdout-days", type=int, default=3)
    s.add_argument("--lookback-days", type=int, default=90)
    s.set_defaults(fn=cmd_trends)

    s = sub.add_parser("ui", help="local dashboard on 127.0.0.1, or --export a read-only HTML snapshot")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--export", metavar="FILE", help="write a self-contained snapshot instead of serving")
    s.add_argument("--days", type=int, default=30, help="window for the snapshot")
    s.add_argument("--no-forecasts", action="store_true", help="skip Toto views in the snapshot")
    s.set_defaults(fn=cmd_ui)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
