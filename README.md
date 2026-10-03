# greenlight

CI/CD observability on OpenTelemetry, built from git, GitHub and your test reports.

greenlight answers one question after every red test run: is this real, or is it the same flaky test again? Around that it tracks flaky tests, CI pipelines, deployments and DORA metrics from git and GitHub, sends all of it to any OpenTelemetry backend as traces and metrics, and gives you a local dashboard and an MCP server, so Claude and ChatGPT (Claude Code, Codex, Claude Desktop, claude.ai, ChatGPT) can ask it before rerunning anything.

It runs on macOS, Linux and Windows (CI tests all three) and in a container.

- [How it works](#how-it-works)
- [Set up](#set-up)
- [Run a server](#run-a-server)
- [Use it from Claude or ChatGPT](#use-it-from-claude-or-chatgpt)
- [OpenTelemetry](#opentelemetry)
- [How auth works](#how-auth-works)
- [GitHub: sync, issues and CI](#github-sync-issues-and-ci)
- [The gate](#the-gate)
- [Dashboard](#dashboard)
- [Configuration](#configuration)
- [Limits](#limits)

## How it works

```
 sources                         greenlight                          out
 ───────                         ──────────                          ───
 JUnit XML (any runner)   ─┐     one database:                ┌─> OTLP traces + metrics
 OTLP test / CI spans     ─┤     your machine, or a server    │   (any OTLP backend)
 GitHub API: Actions,     ─┼──>  gate: PASS / RERUN / REAL ───┤
   PRs, issues, deploys    │     flake stats, quarantine      ├─> dashboard
 git: release notes,      ─┘     pipelines, DORA              ├─> MCP server (agents)
   a test ledger                                              ├─> GitHub issues per flaky test
                                                              └─> PR comment + CI step summary
```

The core idea: **a flaky test is one that both passed and failed on the same code.** That's counting, not machine learning. Every rerun of the same commit is evidence, so greenlight keeps every run and decides what a new failure means from that history.

## Set up

### 1. Install it once

Needs Python 3.11 or newer. The easy way is [uv](https://docs.astral.sh/uv/): it fetches a current Python for you (the `python3` on macOS is 3.9, which is too old) and puts `greenlight` on your PATH for every folder, so there's one install for all your repos.

macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install --python 3.12 "greenlight @ git+https://github.com/rathojohn/greenlight"
uv tool update-shell
```

Windows PowerShell (open a new terminal after the first line so PATH picks up uv):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install --python 3.12 "greenlight @ git+https://github.com/rathojohn/greenlight"
uv tool update-shell
```

`uv tool update-shell` puts uv's tool folder (`~/.local/bin` on macOS and Linux) on your PATH. Open a new terminal afterwards and check which copy runs:

```bash
greenlight --version
```

It should print a version and a path inside the folder `uv tool dir` prints. If it says `command not found`, the new terminal didn't pick up the PATH change yet. If the path points into some other `.venv`, an older install is shadowing this one: run `deactivate`, or open a terminal without that venv active. An old copy also shows up as `invalid choice: 'setup'`.

It doesn't update itself. To get the latest:

```bash
uv tool upgrade greenlight
```

Then restart any open Claude Code or Codex sessions so they load the new MCP server. Nothing else needs redoing.

Installed it into a venv before? Delete that venv once the uv install works, so there's one copy. `greenlight setup` registers whichever copy you run it from.

### 2. Try it on fake data (optional)

`greenlight demo` writes 60 days of runs, pipelines, releases, pull requests and issues into a separate file:

```bash
greenlight demo --db demo.db
greenlight --db demo.db gate
greenlight --db demo.db ui
```

`gate` reports REAL_FAILURE (one stable test broke on the last commit) and exits 1. `ui` opens the dashboard at http://127.0.0.1:8765.

### 3. Point it at a repo

Go to your project's folder (your own clone, wherever it lives, like `cd ~/code/my-app`), then:

```bash
greenlight setup
greenlight auth
greenlight sync
greenlight ui
```

`setup` is safe to run again. It:

- writes `greenlight.toml` with what it can guess from the clone (GitHub repo, release branch or notes, a test ledger), unless there is one already. It has no secrets, so commit it or ignore it, either works.
- registers the MCP server with Claude Code for every project on this machine (`--codex` adds it to Codex too, `--no-claude` skips it). For a team repo, `--project` writes the repo's own MCP config instead: see [Use it from Claude or ChatGPT](#use-it-from-claude-or-chatgpt).
- prints a short rule for `CLAUDE.md` / `AGENTS.md` that tells agents to record test runs and follow the gate (`--agent-rules` appends it for you).

`auth` shows where the GitHub token comes from and what it can do, `sync` pulls PRs, issues, Actions runs and deployments (and exports to OTel if an endpoint is set), and `ui` opens the dashboard.

### 4. Record your test runs

Put `greenlight run --` in front of your test command:

```bash
greenlight run -- pytest
greenlight run -- npm test
```

It runs the tests, records the JUnit report and gates it, then exits 0 for PASS, 2 for RERUN_TARGETED, 1 for REAL_FAILURE, or 3 if something went wrong (like no report). It picks up any JUnit XML the command writes, and adds `--junitxml` for pytest by itself. If the report lands somewhere odd, pass `--junit "reports/*.xml"` or set `[run] junit` in `greenlight.toml`.

Record passing runs too. A test only counts as flaky once the same code has both passed and failed, so the passes are half the evidence. "The same code" is your commit plus any uncommitted edits: rerun without touching anything and a flip shows up, edit a file and that's new code with no history yet.

Any runner that writes JUnit XML works: pytest, vitest `--reporter=junit`, jest with jest-junit, Playwright's junit reporter, `go test` through go-junit-report, Maven Surefire. `greenlight ingest` and `greenlight gate` are the same thing in two steps, for scripts that already have the report.

### 5. Share the history

On its own, each machine keeps its own history in `~/.greenlight`. To share one between CI, your laptop, cloud agent sessions, claude.ai and ChatGPT, [run a server](#run-a-server) and point them at it. Then a test that flaked in CI is known to be flaky everywhere.

## Run a server

A greenlight server is one container holding one database. Everything sends it runs and asks it questions: the GitHub Action, `greenlight run`, agents over MCP, and you through the dashboard. Any host that runs a container with a persistent disk works. It sits idle almost all the time, so the smallest size a host offers is plenty.

| It needs | |
| --- | --- |
| Image | `ghcr.io/rathojohn/greenlight` (amd64 and arm64). The `:toto` tag (amd64 only) adds the forecasts on the Trends page: a 2.2 GB image instead of 400 MB, and it wants 1 GB of memory instead of 512 MB |
| Port | 8000, or `$PORT` when the host sets one |
| Disk | a persistent volume at `/data`, for the database (a few hundred MB at most) |
| Environment | `GREENLIGHT_REPO` (owner/name), `GREENLIGHT_TOKEN` (any long random string, the one password for everything), `GITHUB_TOKEN` (reads the repo; required for a private one) |
| HTTPS | from the host, or a reverse proxy in front |
| Health check | `GET /healthz` |
| Copies | exactly one: the database is SQLite on that disk |

On any machine with Docker, this writes the `.env` file and starts it with `deploy/server/docker-compose.yml`:

```bash
mkdir greenlight-server
cd greenlight-server
curl -fsSLO https://raw.githubusercontent.com/rathojohn/greenlight/main/deploy/server/docker-compose.yml
cat > .env <<EOF
GREENLIGHT_REPO=rathojohn/greenlight
GREENLIGHT_TOKEN=$(openssl rand -hex 32)
GITHUB_TOKEN=$(gh auth token)
EOF
docker compose up -d
docker compose logs greenlight
```

Use your repo instead of `rathojohn/greenlight`. The token line needs the GitHub CLI logged in; for a public repo you can leave `GITHUB_TOKEN` empty. The logs print the server's links, with `$GREENLIGHT_TOKEN` where your token goes (the token itself never lands in a log). The server reads the repo from GitHub (a small clone of its own, refreshed every 10 minutes), so it needs no copy of your code.

Then point everything at it. Set these wherever greenlight runs (your shell profile, the cloud agent environment's settings, CI secrets), with your server's HTTPS address and the token from `.env`:

```bash
export GREENLIGHT_URL=https://greenlight.example.com
export GREENLIGHT_TOKEN=paste-the-token-from-.env
```

| Client | How it uses the server |
| --- | --- |
| `greenlight run`, `greenlight playtest gate` | send each run there and print the decision it makes against all its history |
| The GitHub Action | `server-url` and `server-token` inputs, from secrets: see [CI with the GitHub Action](#ci-with-the-github-action) |
| Claude Code, Codex | `greenlight setup --project --url "$GREENLIGHT_URL"` in the repo, then commit the files: sessions connect with `$GREENLIGHT_TOKEN` instead of starting their own copy |
| claude.ai, ChatGPT | a custom connector with the URL `$GREENLIGHT_URL/<token>/mcp`: see [claude.ai and ChatGPT](#claudeai-and-chatgpt-web-and-phone) |
| The dashboard | `greenlight ui` opens it signed in (a one-time link, good for 2 minutes), or open `$GREENLIGHT_URL` and enter the token on the sign-in page, where a password manager can keep it. A browser stays signed in for a year; the sidebar has Sign out |

Recorded something by mistake? `greenlight forget 82 83` deletes those runs and their results, on the server when `GREENLIGHT_URL` is set (`--dry-run` lists them first). Runs are named by the number the dashboard shows, or by external id.

The database is `/data/<owner>-<name>.db`. To back it up, copy it while the container is stopped, or with `sqlite3 <db> ".backup copy.db"` while it runs.

The same image runs any CLI command in place of the server, like `docker run --rm ghcr.io/rathojohn/greenlight --version`, or the stdio MCP server with `docker run -i --rm ghcr.io/rathojohn/greenlight mcp --repo rathojohn/greenlight`.

## Use it from Claude or ChatGPT

greenlight is an MCP server, so any Claude or ChatGPT app that takes MCP servers can ask it what's flaky, judge a test run, or report on CI and deploys. Pick the row for where you work:

| Where | Do this once | Data comes from |
| --- | --- | --- |
| Claude Code (terminal, desktop, or claude.ai/code on the web) and Codex, in a repo | `greenlight setup --project` in the repo (add `--url "$GREENLIGHT_URL"` with a [server](#run-a-server)), then commit the three files it writes | the checkout, or the server |
| Claude Code or Codex, in every project on your machine | `greenlight setup` (Claude Code), `greenlight setup --codex` (Codex CLI, IDE and the ChatGPT desktop app) | the checkout the agent works in |
| Claude Desktop chat | `greenlight setup --claude-desktop` | GitHub, no clone |
| claude.ai or ChatGPT, on the web or your phone | add your [server](#run-a-server)'s MCP URL as a connector | the server |
| Codex cloud tasks | no MCP servers there yet: install the CLI in the environment's setup script and use `greenlight run` | the task's checkout |

### Claude Code and Codex in a repo

In the repo:

```bash
greenlight setup --project
git add .mcp.json .claude/settings.json .codex/config.toml
git commit -m "Start greenlight in Claude Code and Codex sessions"
```

That writes the repo's own MCP config: `.mcp.json` for Claude Code, `.codex/config.toml` for Codex, and `.claude/settings.json` approving the server and its read-only tools, so nobody gets asked (the tools that change something, like quarantine, still ask). It also adds two hooks: one records token usage after each turn, and one gives subagents the brief (below). `--no-usage-hook` and `--no-brief-hook` leave them out. Each starts greenlight through `uvx`, which downloads it on first use, so nobody installs anything by hand. Anyone with [uv](https://docs.astral.sh/uv/) gets it, and Claude Code on the web already has uv.

That's also why it works in Claude Code on the web: each session is a fresh container, and Claude Code starts project MCP servers before SessionStart hooks run, so a server that a hook installs isn't there yet when Claude looks for it. `uvx` installs it on the spot. Codex only reads `.codex/config.toml` in projects you've marked as trusted.

### Every project on your machine

`greenlight setup` registers the server with Claude Code at user scope, run by the Python greenlight was installed with. By hand, that's:

```bash
claude mcp add --transport stdio --scope user greenlight -- ~/.local/share/uv/tools/greenlight/bin/python -m greenlight.server
```

That path is where `uv tool install` puts it on macOS and Linux. `greenlight setup --dry-run` prints the exact command for your install, Windows included. `greenlight setup --codex` adds the same server to `~/.codex/config.toml`, which the Codex CLI, its IDE extension and the ChatGPT desktop app share.

One server covers every repo: Claude Code tells it which project the session is in, and Codex starts it in the project folder, so it reads that repo's `greenlight.toml`. Restart open sessions after `setup` or an upgrade.

### Claude Desktop

```bash
greenlight setup --claude-desktop --repo rathojohn/greenlight
```

That example adds this repo; give it yours, or run it inside a clone and leave `--repo` off. Quit and reopen Claude Desktop. setup adds one entry per repo (`greenlight-<name>`) to Claude Desktop's config file on macOS or Windows, run by the Python greenlight was installed with, since Claude Desktop doesn't get your shell's PATH. greenlight keeps a small clone of the repo under `~/.greenlight` and refreshes it every 10 minutes.

For a private repo, greenlight needs a token Claude Desktop can see. It asks the GitHub CLI if you're logged in (`gh auth login`), and finds Homebrew's `gh` even though Claude Desktop doesn't have Homebrew on its PATH.

### claude.ai and ChatGPT (web and phone)

These connect to MCP servers by URL, from their own servers, so they need a [greenlight server](#run-a-server) on the internet with HTTPS. Neither can send an API key header, so the token goes in the URL: `$GREENLIGHT_URL/<token>/mcp`, like `https://greenlight.example.com/9f2c4e.../mcp`.

- claude.ai: Settings, Connectors, Add custom connector. It shows up in Claude Desktop too. Free plans get one custom connector.
- ChatGPT: turn on Developer mode (Settings, Security and login), then add it at chatgpt.com/plugins with the plus button. Developer mode is on paid plans, and ChatGPT asks before running a tool that changes something.

That URL is a password: anyone with it can read the repo's test and CI history, quarantine tests, and, if the server's GitHub token can write issues, open flaky-test issues. Restart the server with a new `GREENLIGHT_TOKEN` to revoke it. Clients that can send headers (Claude Code, Codex, the CLI, the Action) use `Authorization: Bearer` on `/mcp` and `/api` instead.

### Codex cloud and other cloud agents

Codex cloud doesn't start MCP servers yet. Install the CLI in the environment's setup script and let the agent rule below do the rest:

```bash
uv tool install "greenlight @ git+https://github.com/rathojohn/greenlight"
```

### What a session starts out knowing

Every session that connects to greenlight gets a short brief in the server's instructions, without calling a tool:

```
greenlight (acme/game), from the last 30 days of test runs and sessions:
- Failing on the default branch, so not caused by your branch: test_checkout_total (since 3f2a1c90). Report these; don't fix them in other work.
- Flaky, passed and failed on the same code: test_search_autocomplete (5/12 commits). If one fails, rerun only it once; don't debug it.
- 2 quarantined tests: the gate ignores their failures.
- After a test run with failures, let the gate judge them before rerunning or debugging (greenlight run, greenlight playtest gate, or the greenlight_triage_run tool).
- When the work moves to a new branch in a long session, tell the user /compact first would cut the cost of everything after it.
- 14% of cost here is file reads kept in context: read files with an offset and limit, or Grep for the lines first.
```

It comes from the same findings as the dashboard's "What to do next", recomputed every minute: what fails on the default branch, which tests are flaky, and the habits that cost the most in this repo (from [token usage](#token-usage-claude-code)). A line only shows up when there's something behind it, so a healthy repo's brief is short or empty.

Subagents, workflow agents included, see CLAUDE.md but not a server's instructions, so `setup --project` adds a SubagentStart hook (`greenlight brief --hook`) that hands them a shorter version. Agents that only search or plan (Explore, Plan) get nothing. `greenlight brief` prints it for scripts, or for an agent prompt you write yourself, and the dashboard shows it under "What Claude starts with".

### What agents should do

This is the rule `setup` prints (and `--agent-rules` writes):

```
## Test failures (greenlight)
Record every test run with greenlight, passing or not (passes are the history that tells a flaky test
from a broken one), and on a failure follow its decision before rerunning anything. In a shell:
`greenlight run -- <test command>` (with GREENLIGHT_URL set, it sends the run to the shared server).
If the MCP server offers greenlight_gate_junit, calling it with the run's JUnit report does the same
(greenlight_playtest_gate for a playtest ledger).
- PASS: nothing that counts failed. Carry on.
- RERUN_TARGETED: only tests with a flake history failed. Rerun just those, once. No full regression.
- REAL_FAILURE: a test with no flake history failed. Investigate it; a full regression only after a fix.
Never quarantine or release a test, or apply issue changes, without saying why.
```

In practice: the agent runs the tests through greenlight, reruns only the flaky ones when that's all that broke, and digs in when something real broke. You can also just ask it things like "what's flaky this week?", "why did CI go red on main?" or "how are deploys looking?" and it answers from the tools below.

| Tool | Writes | Purpose |
| --- | --- | --- |
| greenlight_overview | no | start here: the latest run's decision, flakiest tests, Actions health and DORA in one call |
| greenlight_gate_junit | local DB | record a JUnit report from the project and return the decision, in one call |
| greenlight_triage_run | no | the gate's decision for a run already recorded, with per-failure detail |
| greenlight_playtest_gate | local DB | sync a test ledger in git, then gate the run just made |
| greenlight_list_flaky, greenlight_test_history | no | ranked flaky tests; one test's history |
| greenlight_quarantine / _unquarantine, greenlight_sweep | local DB | manage quarantine |
| greenlight_pipelines | no | workflow health, flaky and slow jobs |
| greenlight_delivery | no | DORA, PR flow, issue backlog |
| greenlight_issues | GitHub, only with apply=true | plan or apply the flaky-test and perf issues |
| greenlight_sync | local DB, reads GitHub | pull everything configured |
| greenlight_duration_regressions, greenlight_suite_forecast | no | Toto forecasts |
| greenlight_token_usage | no | Claude Code tokens per pull request and per failing test |
| greenlight_query | no | read-only SQL over every table |
| greenlight_schema | no | every table and column, and how a dashboard panel's rows are drawn |
| greenlight_dashboards, greenlight_dashboard_save, _delete | the DB, for save and delete | list, build and change [dashboards](#dashboards) |

Read from GitHub (Claude Desktop with `--repo`, claude.ai and ChatGPT connectors, the container), there's no checkout of yours, so the two tools that record a local test run (`greenlight_gate_junit`, `greenlight_playtest_gate`) aren't offered.

## OpenTelemetry

### What greenlight sends

`greenlight otel export` (or `greenlight sync` with an endpoint configured) sends everything new since the last export over OTLP/HTTP. Names follow the OpenTelemetry semantic conventions for [CI/CD](https://opentelemetry.io/docs/specs/semconv/cicd/), [VCS](https://opentelemetry.io/docs/specs/semconv/registry/attributes/vcs/), [test](https://opentelemetry.io/docs/specs/semconv/registry/attributes/test/) and [deployment](https://opentelemetry.io/docs/specs/semconv/registry/attributes/deployment/). Those conventions are still marked development upstream, so names may shift; greenlight's own fields live under `greenlight.*`.

| Trace | Spans | Key attributes |
| --- | --- | --- |
| Pipeline attempt | `RUN <workflow>` (SERVER), `queue`, one span per job, one per step | `cicd.pipeline.name`, `cicd.pipeline.run.id`, `cicd.pipeline.result`, `cicd.pipeline.task.name`, `cicd.pipeline.task.run.result`, `cicd.pipeline.task.type`, `cicd.worker.name`, `vcs.ref.head.name`, `vcs.ref.head.revision` |
| Test run | the run (SERVER), one span per test execution, retries included | `test.suite.name`, `test.suite.run.status`, `test.case.name`, `test.case.result.status`, an `exception` event on failures, `greenlight.gate.decision`, `greenlight.test.classification` (flaky, suspect, stable), `greenlight.test.category` (known_flaky, real_failure...) |
| Deployment | `deploy <environment>` | `deployment.environment.name`, `deployment.name`, `deployment.status`, `greenlight.deploy.commits`, `greenlight.deploy.lead_time_p50_s` |

| Metric | Type | What |
| --- | --- | --- |
| `cicd.pipeline.run.duration` | histogram, s | by pipeline, state (pending = queue, executing) and result |
| `cicd.pipeline.run.errors` | counter | runs that ended in failure, timeout or error |
| `vcs.change.count` | up-down counter | pull requests by state |
| `vcs.change.duration`, `vcs.change.time_to_merge` | gauge, s | open PR age, open-to-merge per PR |
| `greenlight.tests`, `greenlight.tests.quarantined` | gauge | tests by flake classification |
| `greenlight.runs.rerun_ratio` | gauge | share of runs that were reruns of the same code |
| `greenlight.dora.*` | gauge | deployment frequency, lead time p50/p90, change failure rate, time to restore |

Trace ids are derived from the run, so sending the same run twice gives the same trace. Test spans are laid end to end from the run's start, because JUnit records durations but not start times.

### Where to send it

The standard variables decide: `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318`), `OTEL_EXPORTER_OTLP_HEADERS` for auth, `OTEL_SERVICE_NAME` (default: the repo name) and `OTEL_RESOURCE_ATTRIBUTES`. Or set `[otel] endpoint` in `greenlight.toml`. Keys and tokens only ever go in the environment.

Any OTLP/HTTP endpoint works. To browse it locally, `docker compose -f deploy/otel/docker-compose.yml up -d` starts Grafana with Tempo, Prometheus and Loki on http://localhost:3000, keeping what your disk keeps. For anything that doesn't take OTLP over HTTP, put an OpenTelemetry Collector in front of it.

greenlight keeps its own history in SQLite either way, so a short backend retention only limits what you can browse there, not what the gate knows.

Some Tempo (TraceQL) searches to start with:

| Finds | TraceQL |
| --- | --- |
| failed pipeline runs | `{ span.cicd.pipeline.result = "failure" }` |
| test runs with a real failure | `{ span.greenlight.gate.decision = "REAL_FAILURE" }` |
| flaky failures the gate reran | `{ span.greenlight.test.category = "known_flaky" }` |
| slow jobs | `{ span.cicd.pipeline.task.name != "" && duration > 5m }` |

### Receiving spans

If your tests already emit OpenTelemetry, skip JUnit. `greenlight otel receive` listens for OTLP/HTTP JSON on `127.0.0.1:4319` and turns spans carrying `test.case.name` into test results (one run per trace) and `cicd.pipeline.*` spans into pipelines and jobs. `greenlight otel import` does the same from files, including the Collector's file exporter output. Spans already received are skipped, so resending is harmless.

`deploy/otel/collector.yaml` shows the Collector fanning traces out to Grafana and to greenlight, and the Collector's own GitHub receiver, which builds Actions traces from webhooks. That receiver needs GitHub to reach your machine (a tunnel or a public host); `greenlight sync` gets the same runs by polling the API instead.

## How auth works

| Where | Credential | Stored |
| --- | --- | --- |
| GitHub, on your machine | `$GREENLIGHT_GITHUB_TOKEN`, `$GH_TOKEN` or `$GITHUB_TOKEN`, else `gh auth token` if the GitHub CLI is logged in | never: read per command, not written to the DB, config or output |
| GitHub, in Actions | the job's built-in `GITHUB_TOKEN`, scoped by the workflow's `permissions:` | GitHub's |
| OTLP backend | `OTEL_EXPORTER_OTLP_HEADERS` | your environment or a CI secret |
| Dashboard | none on `127.0.0.1`; with `--host`, a token traded once for an HttpOnly, SameSite=Strict cookie | in memory, printed once at start |
| OTLP receiver | none on loopback; `--token` (Bearer) when it listens elsewhere | in memory |
| MCP server over stdio | none: it's a local process your agent starts | |
| greenlight server | `GREENLIGHT_TOKEN`: an `Authorization: Bearer` header, the URL path (`/<token>/mcp`), or a cookie the dashboard's sign-in page sets | the server's environment; never logged |
| Clones greenlight keeps itself | the GitHub token above, handed to git in its environment for each call | never written to disk |

`greenlight auth` prints where the token came from (never the token), the access it has and the rate limit left. Public repos sync without a token at 60 requests an hour.

A fine-grained token scoped to just the repo needs:

| Command | Permissions |
| --- | --- |
| `sync` | Metadata, Contents, Pull requests, Issues, Actions, Deployments: read |
| `issues --apply` | Issues: read and write |
| `ci report --pr-comment` | Pull requests: read and write |

The dashboard binds to 127.0.0.1 by default and rejects requests whose Host header isn't local (DNS rebinding), and writes need an `X-Greenlight` header that a cross-site form can't send. To open it from your phone, run `greenlight ui --host 0.0.0.0` on a network you trust (home LAN, Tailscale): it prints a link with a token, and every request then needs it. It's plain HTTP, so don't expose it to the internet.

## GitHub: sync, issues and CI

### Sync

`greenlight sync` runs every source `greenlight.toml` turns on, each independently, so one failing (rate limit, missing permission) doesn't stop the others:

| Source | From | Needs |
| --- | --- | --- |
| `pulls` | pull requests, newest first, stopping where the last sync left off | token for private repos |
| `issues` | issues, plus the markers on the ones greenlight manages and their labels | same |
| `actions` | every workflow run attempt with its jobs and steps; JUnit reports from artifacts named like `junit*` | same |
| `deployments` | pushes to a branch (repository activity API), GitHub Deployments, GitHub Releases, or files added in git (release notes) | token, except git files |
| `playtest` | a test ledger committed to git (see [Configuration](#configuration)) | a clone |
| `otel` | exports everything new, last, if an endpoint is set | an endpoint |

### Issues

`greenlight issues` plans one GitHub issue per flaky test (flipped on 2+ commits) and per check that ran slower than its baseline; `--apply` does it:

- **create** an issue with the flip rate, recent results by commit, failure messages and what to do.
- **update** it when the numbers change. The body is regenerated each time, so comment below it instead of editing it.
- **reopen** it if the test flips again after you closed it.
- **link** an issue you already opened by hand for the same test, instead of opening a duplicate (`greenlight issues link <number> <test id>` does it explicitly).
- **healed**: one comment once the test has run clean long enough to close. It never closes anything for you.

Add the `quarantined` label to a flaky-test issue and the next sync quarantines the test: it keeps running and being recorded but stops blocking the gate. Remove the label or close the issue to release it. That works from your phone in the GitHub app.

### CI with the GitHub Action

```yaml
permissions:
  contents: read
  pull-requests: write   # one comment per job on the PR

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest --junitxml=reports/junit.xml
        continue-on-error: true            # greenlight decides whether red fails the job
      - id: greenlight
        uses: rathojohn/greenlight@main
        with:
          junit: reports/junit.xml
          name: unit                       # tells matrix jobs apart
          server-url: ${{ secrets.GREENLIGHT_URL }}
          server-token: ${{ secrets.GREENLIGHT_TOKEN }}
          # otlp-endpoint: https://...     # also send this run to your OTel backend
      # only flaky tests failed: rerun just those, and record the rerun as a second attempt on this commit
      - if: steps.greenlight.outputs.decision == 'RERUN_TARGETED'
        env:
          NAMES: ${{ steps.greenlight.outputs.rerun-names }}
        run: pytest -k "$(echo $NAMES | sed 's/ / or /g')" --junitxml=reports/rerun.xml
        continue-on-error: true
      - if: steps.greenlight.outputs.decision == 'RERUN_TARGETED'
        uses: rathojohn/greenlight@main
        with:
          junit: reports/rerun.xml
          name: unit
          server-url: ${{ secrets.GREENLIGHT_URL }}
          server-token: ${{ secrets.GREENLIGHT_TOKEN }}
```

The rerun lands as a second attempt on the same commit, so a pass there is a recorded flip and the second report (and the PR comment) says PASS.

Each job sends its run to your [greenlight server](#run-a-server), which judges it against everything it has seen and answers with the decision. The Action writes that to the job summary, its outputs and a single comment on the pull request. Add the server's address and token as the repo's `GREENLIGHT_URL` and `GREENLIGHT_TOKEN` secrets. Without them, each job is judged on its own results, with no flake history to go on. `fail-on` decides what fails the job: `real` (default, only REAL_FAILURE), `any`, or `never`. A matrix entry counts as its own environment, so a test that always fails on one Python and passes on another isn't called flaky.

Use either the Action or JUnit artifact sync (`[actions] junit_artifacts`) for a given workflow, not both, or each run is counted twice.

## The gate

- A **flip** is a commit where a test both passed and failed. Only commits where it ran 2+ times can flip, and those are the denominator.
- **flaky** means it flipped on 2+ commits in the window (default 30 days); **suspect** means 1. Ranking uses the lower bound of the Wilson interval, so 5 flips out of 10 outranks 1 out of 1.
- Each failure in a run is one of:
  - `quarantined`: ignored.
  - `new_test`: first time it ever ran. Blocks.
  - `known_flaky` / `suspect_flaky`: rerun just that test.
  - `real_failure`: no flake history. Blocks. A flaky test that fails 3+ times on one commit without passing is promoted to this, so a flaky label can't hide a real break.
- The decision is `REAL_FAILURE` if anything blocks, else `RERUN_TARGETED` if something flaky failed, else `PASS`. Only `REAL_FAILURE` justifies a full regression.
- `new_signature: true` means a flaky test failed with a message it hasn't produced before. It still reruns, but look at it.
- `sweep` lists quarantine candidates and quarantined tests with 10+ clean runs in 14 days. It never releases anything by itself.

Agents follow the same rules: see [What agents should do](#what-agents-should-do).

## Dashboard

`greenlight ui` serves it on http://127.0.0.1:8765; `greenlight ui --export snapshot.html` writes a read-only, self-contained copy you can share.

It's dark by default with a light theme one click away, and borrows its look from [beautifului.dev](https://www.beautifului.dev/): cards that lift off the canvas, chips, and a sidebar whose highlight glides. Ctrl+K (or ⌘K, or `/`) searches every page, dashboard, test, pull request, conversation and commit. The time range (24 hours, or 7, 14, 30 or 90 days) applies to every view that has one, and the (i) next to a panel title explains what it counts. Tables sort by any column and page instead of growing. A row opens a side panel with its details, and the list stays where it was: j and k (or the arrows) step through the rows, Esc closes it, and the panel is part of the URL, so Back closes it and a link opens it. Each panel links to the full page for the deep dive. Every list can be filtered: chips for a status (pass, rerun flaky or real failure; flaky, suspect or quarantined; passed or failed CI; open or merged), with their counts, and dropdowns for the rest (branch, pull request, conversation, workflow, source). Filters are part of the URL too.

- **Overview**: the latest verdict, rerun share, time spent rerunning, **what changed** against the period before (Claude's cost, test failures, CI failure rate, each with the conversations, tests or workflows that moved it most and both periods charted), what to do next as cards (most at stake first: tests failing on main, flaky tests Claude spent tokens on, what to quarantine or release, where session cost goes), and the most unstable tests across recent commits (taller bars failed more often; amber means the same commit passed and failed). `greenlight insights` prints the same list.
- **Flaky tests**, **test pages** (results by commit, duration, failure messages, measured numbers against a baseline, linked issues, quarantine controls), **Runs** and **run pages** (why each failure did or didn't block).
- **Commits**: every commit something recorded, with the conversation that made it, its pull request, the gate's decision on its tests, its CI and where it shipped. A **Merges** tab lists each merged pull request with every conversation that had a hand in it and the one that merged it. See [Which conversation it came from](#which-conversation-it-came-from).
- **Pipelines**: runs per day, per-workflow success rate, p50/p95 duration and queue time, flaky and slow jobs, and a job waterfall per run.
- **DORA and GitHub**: the four DORA numbers, the deployments behind them, pull request flow and the issue backlog.
- **Trends**: forecasts from the Toto 2.0 time series model (optional: the server's `:toto` image, or `uv tool install --python 3.12 "greenlight[toto] @ git+https://github.com/rathojohn/greenlight"` on your machine): rerun churn, failure rate and suite duration with a 7-day band, and tests running slower than forecast.
- **Quarantine**: suggestions, what's quarantined, what's clean enough to release.
- **Token usage**: cost per day, where context goes, and what could have been dropped, with tabs for pull requests, tests and sessions (see below).
- **Dashboards** and **SQL**: your own panels over everything above (see below).

### Dashboards

A dashboard is panels of read-only SQL over greenlight's tables, built the way Grafana and Datadog build theirs. The easiest way to get one is to ask Claude ("make me a dashboard of which tests fail most each day, with a branch dropdown"): it reads the tables with `greenlight_schema`, tries the query with `greenlight_query`, and saves it with `greenlight_dashboard_save`, which runs every panel first and saves nothing while one fails. The SQL page and each panel's editor do the same by hand, and the Dashboards page can add a starter dashboard, "What's trending".

- **Panels**: time series (stacked bars, lines or area), stat (one number with its sparkline, its change from the period before, and a status against warn and bad thresholds), top list, table, text, and rows that group and collapse the panels under them.
- **Time**: the time range binds as `:start` and `:end`, and `bucket(time)` groups by hour for 24 hours and by day otherwise. A panel that compares runs its SQL again over the period before: a stat shows the change, a time series a dashed line.
- **Variables**: dropdowns above the panels, each filled by a query (or a list) and bound to every panel as `:name`, NULL for All: `WHERE (:branch IS NULL OR branch = :branch)`. Picks go in the URL, like Grafana's `var-` parameters, so a link keeps them.
- **Events**: deploys show as dashed lines and merges as dots on every time series, so a jump lines up with what shipped.
- **Annotations**: a labeled line on every time series, Grafana's annotations, to see what followed a change. Each is SQL whose rows are (time, label): `SELECT '2026-10-03T00:15:00+00:00', 'Trimmed CLAUDE.md'` marks one moment, and `SELECT merged_at, title FROM pull_requests WHERE title LIKE '%CLAUDE.md%'` finds every one like it. A line lands where it happened inside its hour or day.
- **Reading one**: hovering a chart shows the same time on every other chart. Clicking a legend entry hides that series; the legend shows each series' total, or its average for durations and rates. A ratio like tokens per request doesn't add up, so set the panel's `calc` (Legend shows, in the editor) to mean, max or last. A table cell named `test_id`, `session_id`, `pr`, `sha` or `run_id` opens that thing's side panel.
- **Each panel's menu**: view it larger with its query and rows, edit it, duplicate, copy its rows as CSV, move it, remove it.
- **The dashboard's menu**: settings and variables, and the whole dashboard as JSON, to copy or to paste one in. Auto-refresh and TV mode (no sidebar) are in the toolbar and the URL.

`usd(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cache_write_1h_tokens, model)` prices `agent_usage` tokens in dollars at the model's API list price, and `cost(...)` with the same arguments prices them as input tokens, the way the Token usage page does. A column named `dollars` or ending in `_dollars` or `_usd` shows as dollars, and so does a panel with the `usd` unit. Queries can only read: an SQLite authorizer allows reads and function calls, whatever the connection allows, and stops a query after 5 seconds. Dashboards live in the database, so everyone who opens the server sees the same ones.

## Token usage (Claude Code)

How many tokens went into each pull request, and into each test while it was failing. `greenlight setup --project` adds a hook that runs after every Claude Code turn (`greenlight usage record --hook`). It reads the session's transcript, which Claude Code keeps on disk with the usage of every API call, and records per-minute counts: output, input, cache reads and writes, the model and the git branch (and where context went, below). Code, file contents and the conversation never leave the machine; the one exception is the first line of a session's first prompt, kept as its name (see below). With `GREENLIGHT_URL` set the counts go to the server; otherwise to the local database.

- **A pull request** gets the tokens spent on its branch until it merged or closed. If a later PR reuses the branch name, what comes after goes to that one.
- **A test** gets the tokens a session spent while it was red: from a run in that session that failed it to the next run in the same session that passed it (or the session's end, shown as unfixed). Two tests red at once both count the same tokens, so the per-test numbers don't add up to a total. `greenlight run` and test ledger records note which session ran them, which is what links the two.

### Where the tokens go

Most of what a session costs is context being read again. Every request sends the whole conversation, so anything that lands in context (a test log, a file, a screenshot, Claude's own reply) is paid for again by every later request until the session compacts. The hook also measures that, from the same transcript:

- **Cost** prices every kind of token as input tokens of its model, from Claude's API prices: output 5x, a cache read 0.1x (0.05x on Opus 5.5, 0.025x on Fable 5.1), a cache write 1.25x (2x for the hour-long cache a main session uses). It's a unit every model shares, so sessions compare, and it follows what you pay or what counts against a plan's limits.
- **API price** is the same tokens in dollars at each model's API list price (`usage.PRICES`, from Anthropic's pricing page). On a Pro or Max plan you aren't billed it, but it's the plainest way to compare. A model greenlight has no price for gets no dollars and the default ratios.
- **Where context goes** splits every cache read by what put the tokens there: the system prompt and tools, the conversation itself, file reads, test runs, searches, shell output, images, web pages, each MCP server, subagent reports. Shell commands count by what they do (`cat` is a read, `grep` a search, `pytest` a test run; `[usage] test_commands` in greenlight.toml changes what counts as a test). A result's tokens are estimated from its size with a chars-per-token ratio each session measures from its own context growth (about 2.4 on recent models), and images at width x height / 750.
- **Earlier tasks still in context**: when a session moves to a new branch, the work before it stays in context. greenlight records each switch, how much context the new work inherited and what carrying it cost. In long sessions this is often the biggest number; /compact or a new session per task drops it.
- **Cache rebuilt after idle**: the prompt cache lasts an hour in a main session and 5 minutes in a subagent. The first request after a longer break writes the whole context again.
- **Files read again**: the same file and range read while the first read was still in context and unedited.

- **What rode along** follows each thing on its own: a file, a screenshot, a command's output, a skill, CLAUDE.md, and Claude Code's own notes (like its task list reminders). For each, how many times it entered the context, how many requests read it again after, and what that cost. A screenshot read early in a session is carried by every request until it compacts; CLAUDE.md is in every request of every session and subagent, so it's charged to all of them, at the size each window started with: the transcript has the file Claude Code read after a compaction (and the new one when it changes on disk), and the checkout's reflog says which commit a session started on. A trimmed CLAUDE.md shows up from the window that loaded it, not across the history before it. Items that belong together are summed too: 34 screenshots from one folder, every test run (whatever runs it), one MCP server.
  - **By pull request:** each re-read is charged to the branch of the request that made it, so a pull request's panel shows what rode along while the work was on its branch, and an item's panel shows which pull requests carried it. Something read for one task and carried into the next is split between them by the requests that carried it.
  - **By test:** a test's panel shows what came into the context while it was red in a session (the logs, files and screenshots pulled in while it failed) and what each cost from then on. That's a time window, not a cause: something that arrived while the test was red wasn't necessarily for it.

### Which conversation it came from

The same hook records the git work a session did, so a commit, a merge, a test run or a CI run points back to the conversation behind it:

- **Commits it made.** Claude commits with `-q`, so the output has no sha. The transcript says when each `git commit` (or merge, cherry-pick, revert) ran, and the checkout's reflog says which commit was made then and on which branch, so the sha is exact. Commits made in another checkout aren't found.
- **Pull requests it merged**, from the GitHub MCP tool's result (it has the merge commit) or `gh pr merge` (the number; the synced pull request has the commit).
- **Tests it ran**: runs recorded from a session carry its id.

The Commits page joins all of it on the commit: test runs (including a test ledger's short shas), CI runs, pull requests and deployments. Each commit's panel lists who made, merged or tested it. A conversation in claude.ai opens there; a local one has no page, so its panel shows `claude --resume <id>`, which reopens it from the project's folder on the computer that ran it.

A conversation is named by the first line of its first prompt, cut at about 80 characters, with file paths shortened to their names and anything shaped like a key (a `ghp_` token, a long run of letters and digits) replaced with `[redacted]`. That line is the only prompt text greenlight sends. `[usage] titles = false` keeps it on the machine, and conversations show their ids instead.

What leaves the machine: categories, token numbers, branch names, times, item labels, commit shas and pull request numbers, and each session's title. A label is a path from the repo root, an image's name, a skill's name, or a command's program and subcommand (`npm run test:changed`, `git diff`, `curl`), never its arguments, never anything's contents. `[usage] item_labels = false` in greenlight.toml sends the rest without labels, and `titles = false` without titles.

See it with `greenlight usage`, the Token usage page, or ask an agent (it has `greenlight_token_usage`). `--no-usage-hook` leaves the hook out. To add it by hand, this goes under `hooks` in `.claude/settings.json`:

```json
"Stop": [{"hooks": [{"type": "command", "command": "uvx --from git+https://github.com/rathojohn/greenlight greenlight usage record --hook || true", "timeout": 60}]}]
```

The hook can't hold up a session: it always exits 0, and `|| true` covers uvx itself failing (a Stop hook that exits 2 tells Claude to keep going). The transcript is Claude Code's own file, not a documented interface, so if its format changes, greenlight needs an update to read it. Claude Code's OpenTelemetry export is documented, but it carries no git branch, which is what ties tokens to a PR.

## Configuration

`greenlight.toml` is looked up in this folder or a parent (for the MCP server, the project the agent is working in), then `~/.greenlight/config.toml`; `--config` and `$GREENLIGHT_CONFIG` override. Every key is optional. `greenlight setup` (or just `greenlight init`) writes one.

```toml
db = "~/.greenlight/your-repo.db"

[github]
repo = "owner/name"              # default: parsed from the clone's origin

[git]
path = "."                       # the local clone: release notes, lead time, a test ledger

[actions]
enabled = true
junit_artifacts = "junit*"       # artifact names to ingest as test runs ("" to skip)
days = 30

[delivery]                       # what counts as a deployment: pick one
deploy_branch = "release"        # every push to it (repository activity API)
# deploy_files = "docs/release-notes/*.md"   # every such file added on the default branch
# deploy_environment = "production"          # GitHub Deployments
# deploy_releases = true                     # GitHub Releases
incident_labels = ["incident", "hotfix", "bug"]

[issues]
flaky_label = "flaky-test"
perf_label = "perf-regression"
quarantine_label = "quarantined"
min_flips = 2

[otel]
endpoint = "http://localhost:4318"
# service_name = "your-repo"


[run]
junit = "reports/*.xml"          # what `greenlight run` records (default: any JUnit XML the tests write)

[usage]                          # the Claude Code token usage hook
test_commands = "pytest|make check"  # shell commands that count as test runs (a regex; default covers the usual runners)
item_labels = true               # false: send categories and counts, but no item labels (paths, skills, commands)
titles = true                    # false: don't send each session's first prompt line as its name

[playtest]                       # a test ledger committed to git, see below
enabled = false
```

Read from GitHub (`--repo`), greenlight uses a `greenlight.toml` committed on the default branch if there is one, and otherwise guesses what marks a release the way `init` does: release notes, then a `release` branch, then GitHub Releases.

Environment variables:

| Variable | What |
| --- | --- |
| `GREENLIGHT_REPO` | `owner/name` for `greenlight mcp` and `greenlight serve` to read from GitHub |
| `GREENLIGHT_URL` | a greenlight server for `greenlight run`, `playtest gate` and `ci report` to send runs to |
| `GREENLIGHT_TOKEN` | that server's token: the server reads it, and so do the clients |
| `GREENLIGHT_HOME` | where greenlight keeps its clones, databases and `config.toml` (default `~/.greenlight`; `/data` in the container) |
| `GREENLIGHT_DB` | one database file, overriding everything else |
| `GREENLIGHT_CONFIG` | one `greenlight.toml`, overriding the lookup |
| `GREENLIGHT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | GitHub token (see [How auth works](#how-auth-works)) |
| `GREENLIGHT_GIT_BASE` | where repos are cloned from, for GitHub Enterprise (default `https://github.com`) |
| `PORT` | the HTTP server's port, when a host sets it |

### Test ledgers in git

Some projects run tests outside CI (in coding-agent sessions, on a laptop) and commit a small record of each run instead. `[playtest]` reads one such format, `tools/playtest/runs/*.json`, from every branch: each record names the commit, the uncommitted files it tested (by content hash), and per suite which checks failed or ran slower than a baseline. Records name only failures, so greenlight infers that a check passed when its suite passed, which is exact on the same code. `greenlight playtest gate` syncs and gates the run just made, and prints the command that reruns only the flaky suites. Other ledger formats are a small adapter away (`greenlight/playtest.py` is about 300 lines).

## Limits

- Polling, not streaming: data is as fresh as the last `sync`.
- Read from GitHub (Claude Desktop, connectors, the container), data is up to 10 minutes old, and a fresh server's first answer waits on its first sync (a few seconds for a small repo; a busy one can take a minute, and the tool says to try again).
- Without a server, runs recorded with `greenlight run` stay in the database of the machine that ran them, and CI jobs are judged on their own results.
- A server is one container with SQLite on its disk: one copy, no failover. Back up the database file.
- The OTel CI/CD conventions are still in development upstream, so attribute names may change in later releases.
- One repo per DB. Point `db` at a different file per project.
- `greenlight otel receive` takes JSON only (set `encoding: json` on the Collector's otlphttp exporter); protobuf would need a dependency the CLI otherwise avoids.
- Toto forecasts need about two weeks of daily data before they mean anything.
- A commit made by hand, or in a session without the usage hook, has no conversation. Sessions from before the hook recorded commits are linked to a pull request through its branch, not to its commits.
- No live view of runs in progress, and no log search.
- New tests aren't run several times up front to shake out flakes; a new test that fails just blocks.
- Change failure rate counts incidents from issue labels, not an incident tool.
- Not built: test impact analysis, code coverage, per-test ownership, alerting. For alerts, alert on the exported metrics in your OTel backend.

## License

MIT
