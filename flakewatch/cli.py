"""flakewatch CLI.

  flakewatch init                              write flakewatch.toml for the repo you're in
  flakewatch auth                              where the GitHub token comes from, and what it can do
  flakewatch sync                              pull git and GitHub data into the DB (flakewatch.toml)
  flakewatch ingest --sha $SHA reports/*.xml   record a run
  flakewatch gate --sha $SHA                   exit 0 PASS, 2 RERUN_TARGETED, 1 REAL_FAILURE
  flakewatch playtest gate                     sync the playtest ledger, then gate the run just made
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

from pathlib import Path

from . import analysis, config, playtest
from .db import connect, set_config_db
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
        _print_triage(t)
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


def cmd_init(a: argparse.Namespace) -> int:
    from .gitrepo import Repo
    target = Path(a.path or ".").resolve()
    out = target / config.FILE_NAME
    if out.exists() and not a.force:
        print(f"{out} already exists (--force to overwrite)")
        return 3
    repo = Repo(str(target))
    gh_repo = config.remote_repo(str(target)) if repo.ok() else None
    has_playtest = (target / "tools/playtest/runs").is_dir()
    has_release = bool(repo.ok() and (repo.resolve("refs/remotes/origin/release") or repo.resolve("refs/heads/release")))
    has_notes = (target / "docs/patch-notes").is_dir()
    name = (gh_repo or target.name).split("/")[-1]
    lines = [
        "# flakewatch config. Every key is optional; `flakewatch sync` reads this.",
        "# The GitHub token never goes here: see `flakewatch auth`.",
        f'db = "~/.flakewatch/{name}.db"',
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
        (f'deploy_files = "docs/patch-notes/20??-??-??-*.md"   # each one added on main is a release (git only)'
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
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    print("next: `flakewatch auth` to check GitHub access, then `flakewatch sync` and `flakewatch ui`")
    return 0


def cmd_auth(a: argparse.Namespace) -> int:
    from . import github
    cfg = a.cfg
    token, source = github.resolve_token()
    print(f"repo:  {cfg.repo or 'not set (flakewatch.toml [github] repo, or run inside a GitHub clone)'}")
    print(f"token: {'found via ' + source if token else 'none'}")
    if cfg.repo:
        gh = github.GitHub(cfg.repo, token, cfg.api_url)
        try:
            info = gh.get(f"/repos/{cfg.repo}")
            perms = info.get("permissions") or {}
            level = "admin" if perms.get("admin") else "write" if perms.get("push") else "read" if perms else "public read"
            print(f"access: {level} on {'a private' if info.get('private') else 'a public'} repo")
            limit = gh.get("/rate_limit").get("resources", {}).get("core", {})
            print(f"rate limit: {limit.get('remaining')} of {limit.get('limit')} left this hour")
        except github.GitHubError as e:
            print(f"check failed: {e}")
    print(AUTH_HELP)
    return 0


AUTH_HELP = """
How flakewatch authenticates
  Locally: a token from $FLAKEWATCH_GITHUB_TOKEN, $GH_TOKEN or $GITHUB_TOKEN, else `gh auth token`
  if the GitHub CLI is logged in. It is read per command and never written anywhere.
  In GitHub Actions: the built-in GITHUB_TOKEN, scoped by the workflow's `permissions:` block.
  The dashboard and MCP server are local: they bind to 127.0.0.1 and need no login.

Fine-grained token permissions (Settings > Developer settings > Fine-grained tokens, this repo only)
  sync (read)              Metadata, Contents, Pull requests, Issues, Actions, Deployments: read
  issues --apply           Issues: read and write
  ci report (PR comment)   Pull requests: read and write
  ci record (data branch)  Contents: read and write
"""


def cmd_sync(a: argparse.Namespace) -> int:
    from . import sync
    only = set(a.only.split(",")) if a.only else None
    if only and only - set(sync.SOURCES):
        raise ValueError(f"--only takes {','.join(sync.SOURCES)}")
    with closing(connect(a.db)) as conn:
        report = sync.run(conn, a.cfg, only, log=(lambda m: print(m, file=sys.stderr)) if not a.json else (lambda m: None))
    if a.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"repo {report['repo'] or 'n/a'}, token {report['token']}")
        for source, res in report["sources"].items():
            print(f"  {source:<12} " + ", ".join(f"{k} {v}" for k, v in res.items() if not isinstance(v, (dict, list))))
        for source, err in report["errors"].items():
            print(f"  {source:<12} error: {err}")
    return 1 if report["errors"] else 0


def _playtest_repo(a: argparse.Namespace) -> str:
    return a.repo or a.cfg.git_path or "."


def cmd_playtest_sync(a: argparse.Namespace) -> int:
    with closing(connect(a.db)) as conn:
        res = playtest.sync(conn, _playtest_repo(a))
    print(json.dumps(res, indent=2))
    return 0


def cmd_playtest_gate(a: argparse.Namespace) -> int:
    repo = _playtest_repo(a)
    with closing(connect(a.db)) as conn:
        playtest.sync(conn, repo)
        run_id = playtest.latest_local_run(conn, repo)
        if run_id is None:
            raise LookupError("No playtest record in this checkout yet. Run `npm run test:changed` first.")
        t = analysis.triage_run(conn, run_id=run_id, window_days=a.window_days)
    t["rerun_command"] = playtest.rerun_command(t["rerun_tests"])
    if a.json:
        print(json.dumps(t, indent=2, default=str))
        return t["exit_code"]
    _print_triage(t)
    if t["rerun_command"]:
        print(f"rerun only: {t['rerun_command']}")
    return t["exit_code"]


def _print_triage(t: dict) -> None:
    print(f"{t['decision']}: {t['summary']}")
    for f in t["failures"]:
        extra = f" (flipped on {f['flip_shas']}/{f['eligible_shas']} commits)" if f["flip_shas"] else ""
        print(f"  [{f['category']}] {f['test_id']}{extra}")


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
    p.add_argument("--db", help="SQLite path (default: $FLAKEWATCH_DB, then flakewatch.toml, then ~/.flakewatch/flakewatch.db)")
    p.add_argument("--config", help="flakewatch.toml path (default: $FLAKEWATCH_CONFIG, then this folder or a parent)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write flakewatch.toml for the repo in this folder")
    s.add_argument("path", nargs="?", help="repo folder (default: here)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("auth", help="show where the GitHub token comes from and what it can do")
    s.set_defaults(fn=cmd_auth)

    s = sub.add_parser("sync", help="pull git and GitHub data into the DB, per flakewatch.toml")
    s.add_argument("--only", help="comma list: playtest,pulls,issues,actions,deployments")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("playtest", help="a playtest ledger in git (survive-project's tools/playtest)")
    ps = s.add_subparsers(dest="playtest_cmd", required=True)
    t = ps.add_parser("sync", help="read every run record on every branch")
    t.add_argument("--repo", help="the clone (default: [git] path, or here)")
    t.set_defaults(fn=cmd_playtest_sync)
    t = ps.add_parser("gate", help="sync, then triage the run just made in this checkout")
    t.add_argument("--repo", help="the clone (default: [git] path, or here)")
    t.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_playtest_gate)

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
        a.cfg = config.load(a.config) if a.cmd != "init" else config.Config()
        set_config_db(a.cfg.db)
        return a.fn(a)
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
