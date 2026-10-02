# greenlight

CI/CD observability on OpenTelemetry, built from git, GitHub and test reports. Its core is a gate that decides whether a red test run is a real failure or a known flaky test, built on the rule that a test which both passed and failed on the same code is flaky. Around that: GitHub sync (Actions, PRs, issues, deployments), DORA metrics, OTLP export and receive, a local dashboard, an MCP server, a GitHub Action, and issues per flaky test. The README is the user-facing doc.

## Layout

- `greenlight/schema.sql` + `db.py`: SQLite schema (v5) and migrations. Columns added after a table first shipped go in both `schema.sql` and `db.MIGRATIONS`, and `SCHEMA_VERSION` goes up. Read-only opens upgrade an old DB first.
- `ingest.py`: JUnit XML parsing and `record_run`, the one way runs get written. `ingest_checkout` keys a local run by `gitrepo.Repo.identity` (HEAD plus a hash of uncommitted edits, reports excluded); `greenlight run` and the `greenlight_gate_junit` MCP tool both use it.
- `analysis.py`: flake stats, `triage_run` (the gate), quarantine, sweep, read-only SQL.
- `playtest.py`: adapter for a test ledger committed to git (JSON records in `tools/playtest/runs`, one per run). Infers passes for watched checks; a crashed suite infers nothing.
- `github.py`: stdlib REST client and token resolution. `ghsync.py`: PRs, issues, Actions, deployments. `sync.py`: runs every configured source, each isolated.
- `delivery.py`: pipeline, DORA, PR and issue analytics.
- `issues.py`: plans and applies one GitHub issue per flaky test and perf regression.
- `ci.py` + `action.yml`: record a job's JUnit results, gate (on the server when `server-url` is set), step summary, outputs, PR comment.
- `otel.py`: OTLP/HTTP JSON export (traces and metrics, semantic conventions) and receive/import.
- `config.py`: `greenlight.toml`. `cli.py`: every command. `server.py`: the MCP tools, over stdio (`greenlight mcp`) or inside `hosted.py`. Checkout mode reads the project the agent works in (a user-scoped Claude Code server starts in `~/.claude`, so it finds the project from `CLAUDE_PROJECT_DIR`); repo mode (`--repo`, `GREENLIGHT_REPO`) reads a GitHub repo through `remote.py`. `setup.py`: `greenlight setup` (config, Claude Code user scope, `--project` files, `--claude-desktop`, `--codex`, agent rules).
- `hosted.py`: `greenlight serve`, the hosted server: MCP, the dashboard and its `/api`, `POST /api/records` (runs sent by clients, answered with the gate's decision) and OTLP, on one port behind one token (Bearer header, URL path, or a cookie from the sign-in page at `/login` or a one-time `/login?code=` link that `greenlight ui` opens; `/?token=` still works). One-time codes live in memory, which is fine because the server is one process. `client.py`: `GREENLIGHT_URL` + `GREENLIGHT_TOKEN`; `greenlight run`, `playtest gate` and `ci report` record into a throwaway DB, send the run as a record (`ci.export_run`), and print the server's decision.
- `remote.py`: repo mode. A cache clone per repo under `GREENLIGHT_HOME` (blobless, no checkout), the config for it (a committed greenlight.toml, else guesses), and the `Refresher` that syncs it in the background.
- `Dockerfile` + `docker-entrypoint.sh` + the `container` CI job: the image on ghcr.io, smoke-tested with `.github/scripts/smoke_mcp.py` before it's published. `deploy/server/docker-compose.yml`: the one deploy example. Nothing host-specific goes in this repo: the README documents what any host needs. `web.py` + `dashboard.py` + `ui/index.html`: dashboard and `--export` snapshot.
- `forecast.py`: optional Toto 2.0 forecasts (the `:toto` image).
- `usage.py`: Claude Code token usage. A Stop hook (`greenlight usage record --hook`, added by `setup --project`) reads the session transcript and its subagents', and stores per-minute counts per model and branch (`agent_usage`, `agent_sessions`). PRs get their branch's tokens until merge; tests get a session's tokens while they were red (runs carry the session). `detail()` feeds the dashboard's side panels.
- `insights.py`: what to do next, most at stake first (tests red on the default branch, flaky tests Claude spent tokens on, sweep suggestions, context habits), and `brief()`, the few lines an agent starts with. The server puts the brief in its MCP instructions (a property on the low-level server, refreshed off the request path every minute); subagents don't see server instructions, so `greenlight brief --hook` answers a SubagentStart hook.
- `context.py`: where a session's context went, from the same transcript: per tool category, the tokens results added and the cache reads that carried them until a compaction (`agent_context`, with `system` and `conversation` accounting for the rest of every cache read), cache rebuilds and their cause (`agent_cache_rebuilds`), and branch switches that inherited the earlier work (`agent_task_switches`).
- `deploy/otel/`: local Grafana (otel-lgtm) behind a Collector.
- `demo.py`: synthetic history for every view (`greenlight demo`).

## Commands

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/greenlight demo --db demo.db && .venv/bin/greenlight --db demo.db ui
```

Tests use a fake GitHub (`tests/fakegithub.py`), a fake OTLP collector and a local bare repo standing in for GitHub clones (`GREENLIGHT_GIT_BASE`); nothing calls real GitHub or a real backend. CI runs them on Linux, macOS and Windows. An autouse fixture in `conftest.py` clears every `GREENLIGHT_*` and `OTEL_*` variable, so a shell pointed at a real server or backend never receives test runs (it happened once: `greenlight forget` cleaned it up). Subprocesses get `child_env()` (built from `os.environ`: Python won't start on Windows without `SYSTEMROOT`), and tests that must not find `gh` or `claude` use `hide_clis()`. `tests/test_otel.py` parses every exported payload with the official OTLP protobuf messages (`opentelemetry-proto`, a dev dependency).

## Decisions worth knowing

- History lives in a database: one on each machine, or one shared server. Nothing greenlight records is written to git or GitHub. It only reads from them (records a project commits itself, like a test ledger, are that project's choice). Clients send runs to the server as records keyed by external id, so resending or later syncing the same run adds nothing, and the server recomputes the attempt number against its own history.
- A run's `commit_sha` is the identity of the code tested, not always a plain commit: `<sha>+<hash>` when it had uncommitted edits (playtest records), `<sha>@<name>` for a matrix entry in CI. The plain commit is in `git_commit`. Flips only count on identical code.
- Gate: quarantined failures are ignored, known/suspect flaky failures get a targeted rerun, new tests and tests without flake history block. A flaky test that fails 3+ times on one commit without passing is treated as real. `new_test` only applies to runs that name every result (`total_tests IS NULL`).
- GitHub tokens come from `GREENLIGHT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` or `gh auth token`, and are never stored or printed. Artifact downloads drop the token on the redirect off GitHub.
- Issues carry a hidden marker `<!-- greenlight:flaky:<test id> -->` (or `perf:`); sync reads it into `issues.managed_key`. Issue bodies are regenerated, a stamp-only change is skipped, nothing is ever closed automatically, and a hand-made issue that looks like the same check is linked, not duplicated.
- Quarantine has several writers, each removing only its own rows: manual/auto, `github#<n>` (the quarantined label), `known-failures.json`.
- OTLP: hex trace and span ids derived from the run (deterministic), JSON over HTTP with gzip, standard `OTEL_*` variables. The `otel_exports` table records what has been sent and what has been received, so neither repeats. Semantic convention names are from the development-status CI/CD, VCS, test and deployment registries; greenlight's own attributes are `greenlight.*`.
- The dashboard binds to 127.0.0.1 and checks Host; writes need `X-Greenlight: 1`. Off loopback it needs a token, traded for an HttpOnly SameSite=Strict cookie. The OTLP receiver needs a Bearer token off loopback.
- The dashboard is a panel grid, dark by default (the light theme is a toggle kept in localStorage), with explanations in (i) tooltips instead of paragraphs. API calls use relative URLs so a page served under `/<token>/` keeps working.
- No page grows without end: tables page (`limit` is the page size), long pages split into tabs with their own routes (`#/usage/prs`), and a row opens a side panel (`opt.panel` returns `kind:id`, `PANELS[kind]` builds it). The open panel is a `?panel=` query on the hash, so Back closes it and a link opens it; a hash change that only moves the panel never re-renders the page. GET responses are cached for 30 seconds so tabs and panels don't refetch; refresh and writes clear it.
- Dashboard charts follow the dataviz rules: status colors validated in both themes, rounded bar ends, 2px gaps in stacks, text in ink tokens, the flake grid's bar height carries fail share so amber vs red never rests on hue. Snapshot keys built by `snapKey()` in the page must match `snap_key()` in `web.py`.
- Cost is tokens priced as input tokens (`usage.WEIGHTS`: output 5x, cache read 0.1x, cache write 1.25x, 2x for the 1-hour cache). A tool result's tokens are its size over a chars-per-token ratio each session measures from its own context growth (about 2.4 on recent models; 4 is far off), images width x height / 750. Context only shrinks at a `compact_boundary`, which is what makes "re-read by every later request until a compaction" exact.
- Token usage comes from Claude Code's transcript JSONL (assistant lines' `message.usage`, `gitBranch`, `timestamp`; one response split over several lines repeats its usage, so count each `message.id` once). It's not a documented format, but Claude Code's OpenTelemetry export has no branch. Only counts, models, branches and times are sent. A cloud session's claude.ai id is `CLAUDE_CODE_REMOTE_SESSION_ID` with `cse_` swapped for `session_`; that's what runs record, and `agent_sessions.remote_session` joins it to the transcript's id.
- The CLI imports only the standard library (the Action installs with `--no-deps`); `mcp`/`pydantic` are for the MCP server, Toto is optional.
- Project MCP config (`setup --project`) starts the server with `uvx`, never a binary a SessionStart hook installs: Claude Code starts project MCP servers before hooks run (measured in a cloud session: the servers started 0.1 s before the hook, which took 13 s to install).
- Partial clones fetch blobs one round trip each in `cat-file`, so `Repo.read_blobs` prefetches them in one `git fetch` (68 playtest records: 33 s down to under 1 s). Git calls go through `gitrepo.run_git`, which carries a cache clone's token in the environment, never in a config file.
- The server takes its token in the URL path too because claude.ai and ChatGPT connectors can't send an API key header; access logs are off and the startup lines show `$GREENLIGHT_TOKEN` in place of a token you set, so it never lands in a host's log (only a token the server makes up itself is printed, since there's no other way to learn it). A cookie-authenticated POST also needs `X-Greenlight: 1`, which a cross-site form can't send. The image starts as root only to hand a root-owned `/data` volume to the `greenlight` user, then drops root (`setpriv`).
- Windows: paths handed to git are made relative with forward slashes (`gitrepo._rel`), JSON files are read as `utf-8-sig`, and the Action converts `RUNNER_TEMP` to forward slashes and uses `python` (`python3` can be the Store stub). Subprocesses the server starts get `stdin=DEVNULL`: a stdio server's stdin is the protocol pipe, and on Windows a child inheriting it hangs while another thread reads it. Paths in test TOML go through `as_posix()`, since TOML strings treat a backslash as an escape.

## Writing conventions

- No em dashes anywhere: docs, comments, UI copy, commit messages, replies.
- No stock AI phrasing ("Here's the thing", "It's not X, it's Y").
- UI copy is sentence case with plain verbs.
- No Claude attribution in commits, PRs or comments (no co-author or session lines).
- Name branches for the work (`otel-export`, `fix-gate-retries`), never `claude/*`.
- Merge your own PRs into main as soon as CI is green. Never ask whether to merge.
- Prefer honest trade-offs over hedged or motivational framing.
