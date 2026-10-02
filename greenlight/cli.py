"""greenlight CLI.

  greenlight demo --db demo.db                 synthetic history to try every view on
  greenlight setup                             greenlight.toml, plus the MCP server in Claude Code (--codex too)
  greenlight run -- <test command>             run tests, record the JUnit report, gate it
  greenlight init                              just write greenlight.toml for the repo you're in
  greenlight auth                              where the GitHub token comes from, and what it can do
  greenlight sync                              pull git and GitHub data into the DB (greenlight.toml)
  greenlight ingest --sha $SHA reports/*.xml   record a run
  greenlight gate --sha $SHA                   exit 0 PASS, 2 RERUN_TARGETED, 1 REAL_FAILURE
  greenlight playtest gate                     sync the playtest ledger, then gate the run just made
  greenlight issues [--apply]                  one GitHub issue per flaky test and perf regression
  greenlight ci record|report                  GitHub Actions: record a job, gate it, comment on the PR
  greenlight otel export|receive|import        OpenTelemetry: OTLP traces and metrics out, test and CI/CD spans in
  greenlight flaky                             ranked flaky tests
  greenlight sweep [--apply]                   quarantine candidates / release candidates
  greenlight trends                            Toto duration regressions + rerun forecast
  greenlight ui                                the dashboard (the server's, signed in, when GREENLIGHT_URL is set)
  greenlight usage                             Claude Code tokens per PR and per failing test
  greenlight forget 82 run:6714b3...           delete runs recorded by mistake (--dry-run lists them first)
  greenlight mcp [--repo owner/name]           MCP server over stdio (Claude Code, Codex, Claude Desktop)
  greenlight serve --repo owner/name           MCP server over HTTP (claude.ai and ChatGPT connectors)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
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
    from .setup import config_text
    target = Path(a.path or ".").resolve()
    out = target / config.FILE_NAME
    if out.exists() and not a.force:
        print(f"{out} already exists (--force to overwrite)")
        return 3
    out.write_text(config_text(target), encoding="utf-8")
    print(f"wrote {out}")
    print("next: `greenlight auth` to check GitHub access, then `greenlight sync` and `greenlight ui`")
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    import importlib.util
    from . import remote, setup
    target = Path(a.path or ".").resolve()
    out = target / config.FILE_NAME
    if out.exists():
        print(f"config: {out} already exists; left it alone")
    elif a.dry_run:
        print(f"config: would write {out}")
    else:
        out.write_text(setup.config_text(target), encoding="utf-8")
        print(f"config: wrote {out}")
    if importlib.util.find_spec("mcp") is None:
        print("MCP: the mcp package isn't installed here, so the server can't start. Reinstall greenlight with its "
              "dependencies (no --no-deps).")
    url = a.url or os.environ.get("GREENLIGHT_URL")
    if a.project:
        for line in setup.project_files(target, a.dry_run, url, usage_hook=not a.no_usage_hook,
                                        brief_hook=not a.no_brief_hook):
            print("project: " + line)
        if not a.dry_run and url:
            print(f"project: commit .mcp.json, .claude/settings.json and .codex/config.toml. Sessions connect to {url} "
                  "with $GREENLIGHT_TOKEN: set GREENLIGHT_URL and GREENLIGHT_TOKEN where they run (your shell, or "
                  "the cloud environment's settings).")
        elif not a.dry_run:
            print("project: commit .mcp.json, .claude/settings.json and .codex/config.toml, and every Claude Code "
                  "session on this repo (web included) and every Codex session starts greenlight. They need uv.")
    elif not a.no_claude:
        print("Claude Code: " + setup.claude_mcp(a.dry_run))
    if a.claude_desktop:
        repo = a.repo or config.remote_repo(str(target))
        if not repo:
            raise ValueError("--claude-desktop needs the GitHub repo: run it in a clone, or pass --repo owner/name")
        print("Claude Desktop: " + setup.claude_desktop(remote.check(repo), dry_run=a.dry_run))
    if a.codex:
        print("Codex: " + (f"would add to ~/.codex/config.toml:\n{setup.codex_snippet()}" if a.dry_run else setup.codex_mcp()))
    has_rules = any(setup.RULES_MARKER in (target / n).read_text(encoding="utf-8")
                    for n in ("CLAUDE.md", "AGENTS.md") if (target / n).is_file())
    if a.agent_rules and not a.dry_run:
        for line in setup.write_agent_rules(target):
            print("agent rules: " + line)
    elif not has_rules:
        print("agent rules: add this to CLAUDE.md or AGENTS.md (or rerun with --agent-rules to append it):\n")
        print(setup.AGENT_RULES)
    note = setup.env_note()
    if note:
        print(note)
    print("next: `greenlight auth`, `greenlight sync`, then `greenlight ui`. Tests: `greenlight run -- <test command>`")
    return 0


JUNIT_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache"}


def _fresh_reports(root: str, since: float) -> list[str]:
    """JUnit XML files written under root since the run started, when no --junit was given."""
    import os
    found = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in JUNIT_SKIP]
        for f in files:
            path = os.path.join(d, f)
            if not f.endswith(".xml") or os.path.getmtime(path) < since:
                continue
            with open(path, "rb") as fh:
                if b"<testsuite" in fh.read(4096):
                    found.append(path)
    return sorted(found)


def _with_pytest_report(cmd: list[str], out_dir: str) -> tuple[list[str], str | None]:
    """pytest writes JUnit XML only when asked; ask, unless the command already does."""
    import os
    is_pytest = any(os.path.splitext(os.path.basename(c))[0] == "pytest" for c in cmd[:4])  # also pytest.exe
    if not is_pytest or any(c.startswith("--junitxml") or c.startswith("--junit-xml") for c in cmd):
        return cmd, None
    report = os.path.join(out_dir, "junit.xml")
    return [*cmd, f"--junitxml={report}"], report


def cmd_run(a: argparse.Namespace) -> int:
    import shutil
    import subprocess
    import tempfile
    import time
    import uuid
    from . import ci, client, usage
    from .ingest import ingest_checkout
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        raise ValueError("give the test command after --, e.g. greenlight run -- npm test")
    junit = a.junit
    if not junit and a.cfg.get("run", "junit"):  # [run] junit, relative to greenlight.toml
        conf = a.cfg.get("run", "junit")
        junit = [str(a.cfg.base / g) for g in ([conf] if isinstance(conf, str) else conf)]
    with tempfile.TemporaryDirectory(prefix="greenlight-") as tmp:
        cmd, pytest_report = _with_pytest_report(cmd, tmp) if not junit else (cmd, None)
        before = {f: os.path.getmtime(f) for f in _expand(junit) if os.path.isfile(f)} if junit else {}
        started = time.time() - 1  # coarse mtimes on some filesystems
        code = subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]]).returncode
        if junit:  # only reports this run wrote, never the last run's
            files = [f for f in _expand(junit) if os.path.isfile(f) and before.get(f) != os.path.getmtime(f)]
        elif pytest_report:
            files = [pytest_report] if os.path.isfile(pytest_report) else []
        else:
            files = _fresh_reports(".", started)
        if not files:
            print(f"error: the tests exited {code} but wrote no new JUnit XML greenlight could find. Point --junit at "
                  "the report, or turn on the runner's JUnit reporter.", file=sys.stderr)
            return code or 3
        remote = client.configured()
        ext = f"run:{uuid.uuid4().hex}" if remote else None
        # with a server, the run is recorded in a throwaway DB, sent, and judged there against all its history
        with closing(connect(os.path.join(tmp, "run.db") if remote else a.db)) as conn:
            me = usage.agent_session()  # inside Claude Code, the run remembers the session (tokens per test)
            run_id, _, n = ingest_checkout(conn, files, ".", a.sha, a.branch, source=a.source, external_id=ext,
                                           session=me["remote_session"] or me["session_id"], url=me["url"],
                                           command=" ".join(cmd))
            rec = ci.export_run(conn, run_id) if remote else None
            t = None if remote else analysis.triage_run(conn, run_id=run_id, window_days=a.window_days)
    if remote:
        t = client.submit([rec], gate=ext, window_days=a.window_days)["triage"]
        run_id = t["run"]["run_id"]
    if a.json:
        print(json.dumps({**t, "recorded": {"run_id": run_id, "results": n, "command_exit": code}}, indent=2, default=str))
        return t["exit_code"]
    print(f"greenlight: recorded run {run_id} ({n} results)" + (f" on {remote[0]}" if remote else ""))
    _print_triage(t)
    if t["rerun_tests"]:
        print("rerun only:", " ".join(t["rerun_tests"]))
    if code and t["decision"] == "PASS":
        print(f"note: the command exited {code} with no failing tests in the report; check its output")
        return code
    return t["exit_code"]


def cmd_auth(a: argparse.Namespace) -> int:
    from . import github
    cfg = a.cfg
    token, source = github.resolve_token()
    print(f"repo:  {cfg.repo or 'not set (greenlight.toml [github] repo, or run inside a GitHub clone)'}")
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
How greenlight authenticates
  Locally: a token from $GREENLIGHT_GITHUB_TOKEN, $GH_TOKEN or $GITHUB_TOKEN, else `gh auth token`
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
    import tempfile
    from . import ci, client
    repo = _playtest_repo(a)
    remote = client.configured()
    with tempfile.TemporaryDirectory(prefix="greenlight-") as tmp, \
            closing(connect(os.path.join(tmp, "playtest.db") if remote else a.db)) as conn:
        playtest.sync(conn, repo)
        run_id = playtest.latest_local_run(conn, repo)
        if run_id is None:
            raise LookupError("No playtest record in this checkout yet. Run `npm run test:changed` first.")
        if remote:  # send this checkout's records (the server may not have them yet) and let it judge
            local = Path(repo) / playtest.RUNS_DIR
            ids = {playtest.external_id(f.name) for f in local.glob("*.json")} if local.is_dir() else set()
            ids.add(conn.execute("SELECT external_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])
            rows = conn.execute(f"SELECT run_id FROM runs WHERE external_id IN ({','.join('?' * len(ids))})", sorted(ids))
            records = [ci.export_run(conn, r[0]) for r in rows]
            gate = conn.execute("SELECT external_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        else:
            t = analysis.triage_run(conn, run_id=run_id, window_days=a.window_days)
    if remote:
        t = client.submit(records, gate=gate, window_days=a.window_days)["triage"]
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


def _issue_opts(cfg: config.Config) -> dict:
    return {k: cfg.get("issues", k) for k in ("flaky_label", "perf_label", "quarantine_label", "min_flips",
                                               "window_days", "healed_runs", "healed_days")}


def cmd_issues(a: argparse.Namespace) -> int:
    from . import ghsync, github, issues
    cfg = a.cfg
    gh = github.client(cfg.repo, cfg.api_url, require_token=a.apply) if (cfg.repo or a.apply) else None
    with closing(connect(a.db)) as conn:
        if gh and not a.no_sync:
            try:
                ghsync.sync_issues(conn, gh, cfg.get("issues", "quarantine_label"), int(cfg.get("sync", "days")))
            except github.GitHubError as e:
                if a.apply:
                    raise
                print(f"note: could not refresh issues from GitHub ({e}); planning from the local copy", file=sys.stderr)
        actions = issues.plan(conn, _issue_opts(cfg))
        if not a.apply:
            print(issues.as_json(actions) if a.json else issues.summarize(actions))
            if actions and not a.json:
                print("\ndry run: add --apply to do this on GitHub")
            return 0
        done = issues.apply(conn, gh, actions)
    if a.json:
        print(json.dumps(done, indent=2, default=str))
    else:
        for d in done:
            print(f"{d['kind']:<7} #{d['number'] or '?':<5} {d['result']}  {d['title']}")
    return 1 if any(str(d["result"]).startswith("error") for d in done) else 0


def cmd_issues_link(a: argparse.Namespace) -> int:
    from . import ghsync, github, issues
    gh = github.client(a.cfg.repo, a.cfg.api_url, require_token=True)
    issue = issues.link(gh, a.number, f"{a.kind}:{a.test_id}")
    with closing(connect(a.db)) as conn, conn:
        ghsync.upsert_issue(conn, issue)
    print(f"#{a.number} now tracks {a.kind}:{a.test_id}")
    return 0


def cmd_ci_record(a: argparse.Namespace) -> int:
    from . import ci
    with closing(connect(a.db)) as conn:
        res = ci.record_junit(conn, _expand(a.junit), a.name, a.sha, a.branch)
    print(json.dumps(res))
    return 0


def cmd_ci_report(a: argparse.Namespace) -> int:
    from . import ci, github
    env = ci.actions_env()
    pr = a.pr or (env["pr"] if a.pr_comment else None)
    gh = None
    if a.pr_comment and pr:
        try:
            gh = github.client(a.cfg.repo or env["repo"], a.cfg.api_url, require_token=True)
        except ValueError as e:
            print(f"note: no PR comment: {e}", file=sys.stderr)
    from . import client
    remote = client.configured()
    with closing(connect(a.db, readonly=gh is None and not remote)) as conn:
        run_id = a.run_id or (None if a.sha else ci.this_job_run(conn, a.name))
        if remote:  # the server holds the history: send this job's run and use its decision
            if run_id is None:
                raise LookupError("no run recorded by this job: run `greenlight ci record` first")
            rec = ci.export_run(conn, run_id)
            sent = client.submit([rec], gate=rec["external_id"], window_days=a.window_days)
            res = ci.report(conn, gh=gh, pr=pr, name=a.name, triage=sent["triage"],
                            links={k: int(v) for k, v in (sent.get("issue_links") or {}).items()})
        else:
            res = ci.report(conn, run_id, None if run_id else (a.sha or env["sha"]), a.name, gh, pr, a.window_days)
    if not res["summary_written"]:
        print(res["markdown"])
    if res.get("pr_comment_error"):
        print(f"note: PR comment failed: {res['pr_comment_error']}", file=sys.stderr)
    decision = res["triage"]["decision"]
    if a.fail_on == "never":
        return 0
    if a.fail_on == "any":
        return res["triage"]["exit_code"]
    return 1 if decision == "REAL_FAILURE" else 0


def cmd_otel_export(a: argparse.Namespace) -> int:
    from . import otel
    cfg = a.cfg
    only = set(a.only.split(",")) if a.only else None
    if only and only - {"pipelines", "tests", "deployments", "metrics"}:
        raise ValueError("--only takes pipelines,tests,deployments,metrics")
    with closing(connect(a.db)) as conn:
        run_id = a.run_id
        if a.this_job:
            from . import ci
            run_id = ci.this_job_run(conn, a.name)
            if run_id is None:
                raise LookupError("No run recorded by this job yet. Run `greenlight ci record` first.")
        res = otel.export(conn, cfg.repo, a.endpoint or cfg.get("otel", "endpoint"), cfg.get("otel", "service_name"),
                          only, a.since_days or int(cfg.get("otel", "since_days")), a.all, run_id, a.dry_run,
                          int(cfg.get("otel", "window_days")), cfg.get("delivery", "incident_labels"))
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        verb = "would send" if res.get("dry_run") else "sent"
        print(f"{verb} {res['spans']} spans in {res['traces']} traces and {res['metrics']} metrics to {res['endpoint']}")
        if res.get("rejected_spans") or res.get("rejected_points"):
            print(f"the endpoint rejected {res.get('rejected_spans', 0)} spans and {res.get('rejected_points', 0)} points")
    return 0


def cmd_otel_receive(a: argparse.Namespace) -> int:
    from . import otel
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.token:
        raise ValueError("Binding off loopback needs --token, sent by the exporter as Authorization: Bearer <token>")
    otel.serve(a.db, a.host, a.port, a.token)
    return 0


def cmd_otel_import(a: argparse.Namespace) -> int:
    from . import otel
    with closing(connect(a.db)) as conn:
        for f in _expand(a.files):
            print(f"{f}: {json.dumps(otel.import_file(conn, f))}")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from . import demo
    target = a.demo_db or a.db or "demo.db"
    if Path(target).exists():
        with closing(connect(target, readonly=True)) as conn:
            real = conn.execute("SELECT COUNT(*) FROM runs WHERE COALESCE(source, '') NOT IN ('demo', 'playtest-report') "
                                "OR external_id NOT LIKE 'demo-%'").fetchone()[0]
        if real:
            raise ValueError(f"{target} already holds real runs; pick another file for the demo, e.g. --db demo.db")
    demo.seed(target, a.days, a.seed)
    print(f"next: greenlight --db {target} ui")
    return 0


def _server(a: argparse.Namespace):  # noqa: ANN202
    import os
    if a.db:  # the server opens the DB itself; --db still wins
        os.environ["GREENLIGHT_DB"] = a.db
    from . import server
    server.configure(a.repo, getattr(a, "project", None))
    return server


def cmd_mcp(a: argparse.Namespace) -> int:
    _server(a).run_stdio()
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    _server(a)
    from . import hosted
    hosted.serve_http(a.host, a.port, a.token, a.no_auth)
    return 0


def cmd_usage(a: argparse.Namespace) -> int:
    from . import client, usage
    if a.action == "record":
        return _usage_record(a, client, usage)
    remote = client.configured()
    if remote:
        import urllib.parse
        import urllib.request
        url, token = remote
        req = urllib.request.Request(f"{url}/api/usage?" + urllib.parse.urlencode({"days": a.days}),
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            s = json.loads(r.read())
    else:
        with closing(connect(a.db, readonly=True)) as conn:
            s = usage.summary(conn, a.days)
    if a.json:
        print(json.dumps(s, indent=2, default=str))
        return 0
    t = s["totals"]
    k = lambda n: (f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6  # noqa: E731
                   else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n))
    print(f"Claude Code, last {a.days} days: {t['sessions']} sessions, {t['requests']} requests, "
          f"{k(t['output_tokens'])} output, {k(t['input_tokens'] + t['cache_write_tokens'])} input, "
          f"{k(t['cache_read_tokens'])} cache reads")
    cost = t.get("weighted") or 0
    share = lambda n: f"{n / cost:.0%}" if cost else "0%"  # noqa: E731
    if cost:
        print(f"Cost: {k(cost)} in input tokens (cache reads {share(t['cache_read_tokens'] * usage.WEIGHTS['cache_read_tokens'])}"
              f", output {share(t['output_tokens'] * usage.WEIGHTS['output_tokens'])}"
              f", cache writes {share(cost - t['input_tokens'] - t['cache_read_tokens'] * usage.WEIGHTS['cache_read_tokens'] - t['output_tokens'] * usage.WEIGHTS['output_tokens'])})")
    cx = s.get("context") or {}
    if cx.get("categories"):
        print("\nWhere context goes (share of cost, tokens re-read):")
        for c in cx["categories"][:a.limit]:
            print(f"  {share(c['weighted']):>4}  {k(c['carried_tokens']):>7}  {c['category']}")
        sw, idle = cx.get("task_switches") or {}, next((r for r in cx["rebuilds"]["by_cause"] if r["cause"] == "idle"), None)
        if sw.get("count"):
            print(f"Earlier tasks still in context: {k(sw['carried_tokens'])} re-read ({share(sw['weighted'])} of cost) after "
                  f"{sw['count']} new branch(es); /compact between tasks drops it")
        if idle:
            print(f"Cache rebuilt after idle: {idle['rebuilds']} time(s), {k(idle['tokens'])} tokens ({share(idle['weighted'])})")
        if cx.get("repeat_reads"):
            print(f"Files read again while still in context: {cx['repeat_reads']} ({k(cx['repeat_tokens'])} tokens)")
    if s["by_pr"]:
        print("\nBy pull request (cost / output / cache reads):")
        for p in s["by_pr"][:a.limit]:
            print(f"  #{p['number']:<5} {k(p.get('weighted', 0)):>6} {k(p['output_tokens']):>6} "
                  f"{k(p['cache_read_tokens']):>7}  {p['title']}")
    if s["by_test"]:
        print("\nWhile a test was red (cost / output / cache reads, times red):")
        for x in s["by_test"][:a.limit]:
            print(f"  {k(x.get('weighted', 0)):>6} {k(x['output_tokens']):>6} "
                  f"{k(x['cache_read_tokens']):>7}  {x['times_red']}x  {x['test_id']}")
    if not t["requests"]:
        print("No usage recorded yet. `greenlight setup --project` adds the hook that records it.")
    return 0


def cmd_insights(a: argparse.Namespace) -> int:
    """What to do next, from the server when GREENLIGHT_URL is set."""
    from . import client, insights
    if client.configured() and not a.local:
        d = client.get("/api/insights", {"days": a.days})
    else:
        with closing(connect(a.db, readonly=True)) as conn:
            found = insights.insights(conn, a.days)
            d = {"insights": [{k: v for k, v in i.items() if k not in ("red", "rank")} for i in found],
                 "brief": insights.brief(conn, a.days, found=found)}
    if a.json:
        print(json.dumps(d, indent=2, default=str))
        return 0
    if not d["insights"]:
        print(f"Nothing to act on in the last {a.days} days.")
    for i in d["insights"][:a.limit]:
        print(f"[{i['severity']}] {i['title']}\n        {i['detail']}")
    return 0


# Subagents that only search or plan: a brief about tests is noise for them
QUIET_AGENTS = {"Explore", "Plan", "claude-code-guide", "statusline-setup", "output-style-setup"}


def cmd_brief(a: argparse.Namespace) -> int:
    """The brief an agent starts with. --hook: Claude Code's SessionStart or SubagentStart hook, which answers as
    hook JSON and never fails the session."""
    from . import client, insights
    try:
        hook = json.loads(sys.stdin.read() or "{}") if a.hook else {}
        event = hook.get("hook_event_name") or ("SubagentStart" if a.subagent else "SessionStart")
        subagent = a.subagent or event == "SubagentStart"
        if subagent and hook.get("agent_type") in QUIET_AGENTS:
            return 0
        if client.configured() and not a.local:
            text = client.get("/api/brief", {"days": a.days, **({"subagent": 1} if subagent else {})},
                              timeout=8 if a.hook else 60)["brief"]
        else:
            with closing(connect(a.db, readonly=True)) as conn:
                text = insights.brief(conn, a.days, a.cfg.repo, subagent=subagent)
        if text and a.hook:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
        elif text:
            print(text)
    except Exception as e:  # noqa: BLE001 - a hook must never hold up a session
        if not a.hook:
            raise
        print(f"greenlight brief: {e}", file=sys.stderr)
    return 0


def _usage_record(a: argparse.Namespace, client, usage) -> int:  # noqa: ANN001
    """The Stop hook: never block or fail the session. Problems go to stderr and the exit code stays 0."""
    try:
        hook = json.loads(sys.stdin.read() or "{}") if a.hook else {}
        if a.transcript:
            hook = {**hook, "transcript_path": a.transcript, "session_id": a.session or Path(a.transcript).stem,
                    "cwd": hook.get("cwd") or os.getcwd()}
        payload = usage.payload_from_hook(hook)
        if not payload:
            raise ValueError("no transcript: run it as a Claude Code hook, or pass --transcript")
        remote = client.configured()
        if remote:
            client.send_usage(payload)
        else:
            with closing(connect(a.db)) as conn:
                usage.store(conn, payload)
        if not a.hook:
            print(f"recorded {sum(r['requests'] for r in payload['rows'])} requests from session "
                  f"{payload['session']['session_id']}" + (f" on {remote[0]}" if remote else ""))
        return 0
    except Exception as e:  # noqa: BLE001 - a hook must never break the session
        print(f"greenlight usage: {e}", file=sys.stderr)
        return 0 if a.hook else 1


def cmd_forget(a: argparse.Namespace) -> int:
    from . import client
    remote = None if a.local else client.configured()
    if remote:
        gone = client.forget(a.runs, a.dry_run)
    else:
        with closing(connect(a.db)) as conn:
            gone = analysis.forget_runs(conn, a.runs, a.dry_run)
    verb = "would forget" if a.dry_run else "forgot"
    for g in gone:
        print(f"{verb} run {g['run_id']} ({g['external_id']}, commit {str(g['commit_sha'])[:8]}, {g['started_at']})")
    where = f" on {remote[0]}" if remote else ""
    print(f"{len(gone)} of {len(a.runs)} {'run' if len(a.runs) == 1 else 'runs'} {verb}{where}"
          + ("; the rest matched nothing" if len(gone) < len(a.runs) else ""))
    return 0


def cmd_ui(a: argparse.Namespace) -> int:
    import webbrowser

    from . import client, web
    remote = None if (a.local or a.export or a.db) else client.configured()
    if remote:  # with a server, its dashboard is the one with the history: open it signed in
        link = client.login_link()
        if a.no_browser or not webbrowser.open(link):
            print(f"Open this within 2 minutes to sign in (it works once): {link}")
        else:
            print(f"Opened the dashboard on {remote[0]}")
        return 0
    web.set_incident_labels(a.cfg.get("delivery", "incident_labels"))
    if a.export:
        n = web.export_snapshot(a.db, a.export, a.days, with_forecasts=not a.no_forecasts)
        print(f"wrote {a.export}: {n['responses']} views ({n['skipped']} could not be built and show an explanation)")
        return 0
    web.serve(a.db, a.port, open_browser=not a.no_browser, host=a.host, token=a.token)
    return 0


def _version() -> str:
    """Version plus where this copy lives, so a stale install shadowing a new one is easy to spot."""
    from importlib import metadata
    try:
        v = metadata.version("greenlight")
    except metadata.PackageNotFoundError:
        v = "unknown"
    return f"greenlight {v} ({Path(__file__).resolve().parent}, Python {sys.version.split()[0]})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="greenlight", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=_version())
    p.add_argument("--db", help="SQLite path (default: $GREENLIGHT_DB, then greenlight.toml, then ~/.greenlight/greenlight.db)")
    p.add_argument("--config", help="greenlight.toml path (default: $GREENLIGHT_CONFIG, then this folder or a parent)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="write 60 days of synthetic history to try every view on")
    s.add_argument("--db", dest="demo_db", help="where to write it (default: demo.db)")
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("init", help="write greenlight.toml for the repo in this folder")
    s.add_argument("path", nargs="?", help="repo folder (default: here)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("setup", help="greenlight.toml, plus the MCP server in Claude Code, Codex or Claude Desktop")
    s.add_argument("path", nargs="?", help="repo folder (default: here)")
    s.add_argument("--project", action="store_true", help="write the repo's own .mcp.json, .claude/settings.json and "
                   ".codex/config.toml (commit them) instead of registering at user scope")
    s.add_argument("--claude-desktop", action="store_true", help="add the server to Claude Desktop, reading this repo "
                   "from GitHub")
    s.add_argument("--repo", help="owner/name for --claude-desktop (default: this clone's GitHub repo)")
    s.add_argument("--url", help="your greenlight server, for --project: sessions connect to it instead of starting "
                                 "their own copy (default: $GREENLIGHT_URL)")
    s.add_argument("--codex", action="store_true", help="also add the server to ~/.codex/config.toml (Codex CLI, IDE "
                   "and ChatGPT desktop)")
    s.add_argument("--agent-rules", action="store_true", help="append the gate rule to CLAUDE.md / AGENTS.md")
    s.add_argument("--no-usage-hook", action="store_true", help="for --project: skip the hook that records Claude "
                   "Code token usage after each turn")
    s.add_argument("--no-brief-hook", action="store_true", help="for --project: skip the hook that gives subagents "
                                                                 "the brief")
    s.add_argument("--no-claude", action="store_true", help="skip registering with Claude Code")
    s.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("run", help="run tests, record the JUnit report and gate it: greenlight run -- <test command>")
    s.add_argument("--junit", nargs="+", help="report files or globs (default: [run] junit, else any JUnit XML the "
                                              "command writes; pytest gets --junitxml added)")
    s.add_argument("--sha", help="default: HEAD, plus a hash of uncommitted edits")
    s.add_argument("--branch")
    s.add_argument("--source", default="local", help="local, agent, ci...")
    s.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    s.add_argument("--json", action="store_true")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("auth", help="show where the GitHub token comes from and what it can do")
    s.set_defaults(fn=cmd_auth)

    s = sub.add_parser("sync", help="pull git and GitHub data into the DB, per greenlight.toml")
    s.add_argument("--only", help="comma list: playtest,records,pulls,issues,actions,deployments,otel")
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

    s = sub.add_parser("issues", help="plan (or --apply) one GitHub issue per flaky test and perf regression")
    s.add_argument("--apply", action="store_true", help="create, update and reopen issues on GitHub")
    s.add_argument("--no-sync", action="store_true", help="plan from the local copy without refreshing issues")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_issues)
    isub = s.add_subparsers(dest="issues_cmd")
    t = isub.add_parser("link", help="mark an existing issue as the one tracking a test")
    t.add_argument("number", type=int)
    t.add_argument("test_id")
    t.add_argument("--kind", choices=["flaky", "perf"], default="flaky")
    t.set_defaults(fn=cmd_issues_link)

    s = sub.add_parser("ci", help="GitHub Actions: record a job's results, then gate and report on the PR")
    csub = s.add_subparsers(dest="ci_cmd", required=True)
    t = csub.add_parser("record", help="record this job's JUnit results")
    t.add_argument("--junit", nargs="+", required=True, help="JUnit XML files or globs")
    t.add_argument("--name", help="tells matrix jobs apart, e.g. py3.12")
    t.add_argument("--sha", help="default: $GITHUB_SHA")
    t.add_argument("--branch")
    t.set_defaults(fn=cmd_ci_record)
    t = csub.add_parser("report", help="gate (on the server, with GREENLIGHT_URL set), then write the step summary, "
                                       "outputs and PR comment")
    t.add_argument("--sha", help="default: $GITHUB_SHA")
    t.add_argument("--run-id", type=int)
    t.add_argument("--name")
    t.add_argument("--pr", type=int, help="default: the pull request that triggered the workflow")
    t.add_argument("--pr-comment", action="store_true", help="add or update a comment on the pull request")
    t.add_argument("--fail-on", choices=["real", "any", "never"], default="real",
                   help="real: exit 1 only on REAL_FAILURE (default); any: gate exit codes; never: always 0")
    t.add_argument("--window-days", type=int, default=analysis.DEFAULT_WINDOW_DAYS)
    t.set_defaults(fn=cmd_ci_report)

    s = sub.add_parser("otel", help="OpenTelemetry: export traces and metrics over OTLP, or receive test and CI/CD spans")
    osub = s.add_subparsers(dest="otel_cmd", required=True)
    t = osub.add_parser("export", help="send pipelines, test runs and deployments as traces, plus metrics, over OTLP/HTTP")
    t.add_argument("--endpoint", help="OTLP/HTTP base URL (default: $OTEL_EXPORTER_OTLP_ENDPOINT, [otel] endpoint, "
                                      "http://localhost:4318)")
    t.add_argument("--only", help="comma list: pipelines,tests,deployments,metrics")
    t.add_argument("--since-days", type=int, help="how far back to look for unsent items (default 90)")
    t.add_argument("--all", action="store_true", help="resend items already sent")
    t.add_argument("--run-id", type=int, help="send just this test run")
    t.add_argument("--this-job", action="store_true", help="in GitHub Actions: send the run this job recorded")
    t.add_argument("--name", help="with --this-job: the matrix name given to `ci record`")
    t.add_argument("--dry-run", action="store_true", help="count what would be sent, send nothing")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_otel_export)
    t = osub.add_parser("receive", help="accept OTLP/HTTP JSON test and CI/CD spans into the DB")
    t.add_argument("--host", default="127.0.0.1")
    t.add_argument("--port", type=int, default=4319, help="default 4319, beside a Collector on 4318")
    t.add_argument("--token", help="required when --host isn't loopback")
    t.set_defaults(fn=cmd_otel_receive)
    t = osub.add_parser("import", help="load OTLP JSON files (one request, or one per line as the Collector writes)")
    t.add_argument("files", nargs="+")
    t.set_defaults(fn=cmd_otel_import)

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

    s = sub.add_parser("mcp", help="the MCP server over stdio (what Claude Code, Codex and Claude Desktop start)")
    s.add_argument("--repo", help="owner/name: read it from GitHub, no clone needed (default: $GREENLIGHT_REPO, "
                                  "else the checkout you're in)")
    s.add_argument("--project", help="the checkout to work in (default: $CLAUDE_PROJECT_DIR, else here)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("serve", help="the hosted server: MCP over HTTP, the dashboard and the ingest API on one port")
    s.add_argument("--repo", help="owner/name to read from GitHub (default: $GREENLIGHT_REPO, else the checkout you're in)")
    s.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 in a container or behind a host")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000),
                   help="default: $PORT (hosts like Render and Cloud Run set it), else 8000")
    s.add_argument("--token", help="the one secret for everything: Authorization: Bearer, the URL path (/<token>/mcp) "
                                   "or the dashboard's sign-in page (default: $GREENLIGHT_TOKEN, else a random one "
                                   "off loopback)")
    s.add_argument("--no-auth", action="store_true", help="no token at all. Only for a public repo you don't mind "
                                                          "anyone reading through this server")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("usage", help="Claude Code tokens per pull request and per failing test (record: the hook "
                                     "that collects them)")
    s.add_argument("action", nargs="?", choices=["show", "record"], default="show")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--limit", type=int, default=15, help="rows per list")
    s.add_argument("--json", action="store_true")
    s.add_argument("--hook", action="store_true", help="record: read Claude Code's hook input on stdin; never fail")
    s.add_argument("--transcript", help="record: a session transcript (.jsonl) instead of hook input")
    s.add_argument("--session", help="record: the session id to file it under (default: from the transcript)")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("insights", help="what to do next, most at stake first (from the server when GREENLIGHT_URL "
                                        "is set)")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.add_argument("--local", action="store_true", help="read the local database even with GREENLIGHT_URL set")
    s.set_defaults(fn=cmd_insights)

    s = sub.add_parser("brief", help="the short brief an agent starts work with: what fails on the default branch, "
                                     "flaky tests, costly habits")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--hook", action="store_true", help="answer a Claude Code SessionStart or SubagentStart hook "
                                                       "(hook input on stdin); never fail")
    s.add_argument("--subagent", action="store_true", help="the shorter brief for a subagent")
    s.add_argument("--local", action="store_true", help="read the local database even with GREENLIGHT_URL set")
    s.set_defaults(fn=cmd_brief)

    s = sub.add_parser("forget", help="delete runs recorded by mistake, by number or external id (on the server "
                                      "when GREENLIGHT_URL is set)")
    s.add_argument("runs", nargs="+", help="run numbers as the dashboard shows them, or external ids")
    s.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    s.add_argument("--local", action="store_true", help="this machine's database, even with GREENLIGHT_URL set")
    s.set_defaults(fn=cmd_forget)

    s = sub.add_parser("ui", help="the dashboard: the server's, signed in, when GREENLIGHT_URL is set; else a local "
                                  "one on 127.0.0.1, or --export a read-only HTML snapshot")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address. Anything but loopback (e.g. 0.0.0.0 for your phone on the LAN) needs a token")
    s.add_argument("--token", help="access token when --host isn't loopback (default: a random one, printed)")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--export", metavar="FILE", help="write a self-contained snapshot instead of serving")
    s.add_argument("--days", type=int, default=30, help="window for the snapshot")
    s.add_argument("--no-forecasts", action="store_true", help="skip Toto views in the snapshot")
    s.add_argument("--local", action="store_true",
                   help="this machine's database, even with GREENLIGHT_URL set (which otherwise opens the server's)")
    s.set_defaults(fn=cmd_ui)

    a = p.parse_args(argv)
    if sys.platform == "win32":  # a redirected stream there uses the legacy code page: don't crash on a test name
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
    try:
        a.cfg = config.load(a.config) if a.cmd not in ("init", "setup", "mcp", "serve") else config.Config()
        set_config_db(a.cfg.db)
        return a.fn(a)
    except (LookupError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
