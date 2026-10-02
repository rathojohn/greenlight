# greenlight

CI/CD observability on OpenTelemetry, built from git, GitHub and test reports. Its core is a gate that decides whether a red test run is a real failure or a known flaky test, built on the rule that a test which both passed and failed on the same code is flaky. Around that: GitHub sync (Actions, PRs, issues, deployments), DORA metrics, OTLP export and receive, a local dashboard, an MCP server, a GitHub Action, and issues per flaky test. The README is the user-facing doc.

## Layout

- `greenlight/schema.sql` + `db.py`: SQLite schema (v3) and migrations. Columns added after a table first shipped go in both `schema.sql` and `db.MIGRATIONS`, and `SCHEMA_VERSION` goes up. Read-only opens upgrade an old DB first.
- `ingest.py`: JUnit XML parsing and `record_run`, the one way runs get written. `ingest_checkout` keys a local run by `gitrepo.Repo.identity` (HEAD plus a hash of uncommitted edits, reports excluded); `greenlight run` and the `greenlight_gate_junit` MCP tool both use it.
- `analysis.py`: flake stats, `triage_run` (the gate), quarantine, sweep, read-only SQL.
- `playtest.py`: adapter for a test ledger committed to git (survive-project's `tools/playtest/runs`). Infers passes for watched checks; a crashed suite infers nothing.
- `github.py`: stdlib REST client and token resolution. `ghsync.py`: PRs, issues, Actions, deployments. `sync.py`: runs every configured source, each isolated.
- `delivery.py`: pipeline, DORA, PR and issue analytics.
- `issues.py`: plans and applies one GitHub issue per flaky test and perf regression.
- `ci.py` + `action.yml`: GitHub Actions history on the `greenlight-data` branch, gate, step summary, PR comment.
- `otel.py`: OTLP/HTTP JSON export (traces and metrics, semantic conventions) and receive/import.
- `config.py`: `greenlight.toml`. `cli.py`: every command. `server.py`: MCP over stdio (`greenlight mcp`) and streamable HTTP (`greenlight serve`). Checkout mode reads the project the agent works in (a user-scoped Claude Code server starts in `~/.claude`, so it finds the project from `CLAUDE_PROJECT_DIR`); repo mode (`--repo`, `GREENLIGHT_REPO`) reads a GitHub repo through `remote.py`. `setup.py`: `greenlight setup` (config, Claude Code user scope, `--project` files, `--claude-desktop`, `--codex`, agent rules).
- `remote.py`: repo mode. A cache clone per repo under `GREENLIGHT_HOME` (blobless, no checkout), the config for it (a committed greenlight.toml, else guesses), and the `Refresher` that syncs it in the background.
- `Dockerfile` + the `container` CI job: the image on ghcr.io, smoke-tested with `.github/scripts/smoke_mcp.py` before it's published. `web.py` + `dashboard.py` + `ui/index.html`: dashboard and `--export` snapshot.
- `forecast.py`: optional Toto 2.0 forecasts.
- `deploy/otel/`: local Grafana (otel-lgtm) behind a Collector. `integrations/survive-project/`: worked example.
- `demo.py`: synthetic history for every view (`greenlight demo`).

## Commands

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/greenlight demo --db demo.db && .venv/bin/greenlight --db demo.db ui
```

Tests use a fake GitHub (`tests/fakegithub.py`), a fake OTLP collector and a local bare repo standing in for GitHub clones (`GREENLIGHT_GIT_BASE`); nothing calls real GitHub or a real backend. CI runs them on Linux, macOS and Windows. Subprocesses get `child_env()` (built from `os.environ`: Python won't start on Windows without `SYSTEMROOT`), and tests that must not find `gh` or `claude` use `hide_clis()`. `tests/test_otel.py` parses every exported payload with the official OTLP protobuf messages (`opentelemetry-proto`, a dev dependency).

## Decisions worth knowing

- A run's `commit_sha` is the identity of the code tested, not always a plain commit: `<sha>+<hash>` when it had uncommitted edits (playtest records), `<sha>@<name>` for a matrix entry in CI. The plain commit is in `git_commit`. Flips only count on identical code.
- Gate: quarantined failures are ignored, known/suspect flaky failures get a targeted rerun, new tests and tests without flake history block. A flaky test that fails 3+ times on one commit without passing is treated as real. `new_test` only applies to runs that name every result (`total_tests IS NULL`).
- GitHub tokens come from `GREENLIGHT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` or `gh auth token`, and are never stored or printed. Artifact downloads drop the token on the redirect off GitHub.
- Issues carry a hidden marker `<!-- greenlight:flaky:<test id> -->` (or `perf:`); sync reads it into `issues.managed_key`. Issue bodies are regenerated, a stamp-only change is skipped, nothing is ever closed automatically, and a hand-made issue that looks like the same check is linked, not duplicated.
- Quarantine has several writers, each removing only its own rows: manual/auto, `github#<n>` (the quarantined label), `known-failures.json`.
- OTLP: hex trace and span ids derived from the run (deterministic), JSON over HTTP with gzip, standard `OTEL_*` variables. The `otel_exports` table records what has been sent and what has been received, so neither repeats. Semantic convention names are from the development-status CI/CD, VCS, test and deployment registries; greenlight's own attributes are `greenlight.*`.
- The dashboard binds to 127.0.0.1 and checks Host; writes need `X-Greenlight: 1`. Off loopback it needs a token, traded for an HttpOnly SameSite=Strict cookie. The OTLP receiver needs a Bearer token off loopback.
- Dashboard charts follow the dataviz rules: status colors validated in both themes, rounded bar ends, 2px gaps in stacks, text in ink tokens, the flake grid's bar height carries fail share so amber vs red never rests on hue. Snapshot keys built by `snapKey()` in the page must match `snap_key()` in `web.py`.
- The CLI imports only the standard library (the Action installs with `--no-deps`); `mcp`/`pydantic` are for the MCP server, Toto is optional.
- Project MCP config (`setup --project`) starts the server with `uvx`, never a binary a SessionStart hook installs: Claude Code starts project MCP servers before hooks run (measured in a cloud session: the servers started 0.1 s before the hook, which took 13 s to install).
- Partial clones fetch blobs one round trip each in `cat-file`, so `Repo.read_blobs` prefetches them in one `git fetch` (68 playtest records: 33 s down to under 1 s). Git calls go through `gitrepo.run_git`, which carries a cache clone's token in the environment, never in a config file.
- The HTTP server takes its token in the URL path because claude.ai and ChatGPT connectors can't send an API key header; access logs are off so the token never lands in a log.
- Windows: paths handed to git are made relative with forward slashes (`gitrepo._rel`), JSON files are read as `utf-8-sig`, and the Action converts `RUNNER_TEMP` to forward slashes and uses `python` (`python3` can be the Store stub). Subprocesses the server starts get `stdin=DEVNULL`: a stdio server's stdin is the protocol pipe, and on Windows a child inheriting it hangs while another thread reads it. Paths in test TOML go through `as_posix()`, since TOML strings treat a backslash as an escape.

## Writing conventions

- No em dashes anywhere: docs, comments, UI copy, commit messages, replies.
- No stock AI phrasing ("Here's the thing", "It's not X, it's Y").
- UI copy is sentence case with plain verbs.
- No Claude attribution in commits, PRs or comments (no co-author or session lines).
- Name branches for the work (`otel-export`, `fix-gate-retries`), never `claude/*`.
- Merge your own PRs into main as soon as CI is green. Never ask whether to merge.
- Prefer honest trade-offs over hedged or motivational framing.
