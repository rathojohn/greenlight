# greenlight

CI/CD observability on OpenTelemetry, built from git, GitHub and your test reports.

greenlight answers one question after every red test run: is this real, or is it the same flaky test again? Around that it tracks flaky tests, CI pipelines, deployments and DORA metrics from git and GitHub, sends all of it to any OpenTelemetry backend as traces and metrics, and gives you a local dashboard and an MCP server, so Claude and ChatGPT (Claude Code, Codex, Claude Desktop, claude.ai, ChatGPT) can ask it before rerunning anything.

It runs on macOS, Linux and Windows (CI tests all three) and in a container.

- [How it works](#how-it-works)
- [Set up](#set-up)
- [Use it from Claude or ChatGPT](#use-it-from-claude-or-chatgpt)
- [Run it in a container](#run-it-in-a-container)
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
 JUnit XML (any runner)   ─┐                                  ┌─> OTLP traces + metrics
 OTLP test / CI spans     ─┤     SQLite index                 │   (Grafana, Tempo, Jaeger,
 GitHub API: Actions,     ─┼──>  gate: PASS / RERUN / REAL ───┤    Honeycomb, SigNoz, ...)
   PRs, issues, deploys    │     flake stats, quarantine      ├─> local dashboard
 git: run records on a    ─┤     pipelines, DORA              ├─> MCP server (agents)
   data branch, release    │                                  ├─> GitHub issues per flaky test
   notes, a test ledger   ─┘                                  └─> PR comment + CI step summary
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

### 5. Add it to CI

See [CI with the GitHub Action](#ci-with-the-github-action). CI runs land in the same history, so a test that flaked in CI is known to be flaky on your laptop too, once you `sync`.

## Use it from Claude or ChatGPT

greenlight is an MCP server, so any Claude or ChatGPT app that takes MCP servers can ask it what's flaky, judge a test run, or report on CI and deploys. Pick the row for where you work:

| Where | Do this once | Data comes from |
| --- | --- | --- |
| Claude Code (terminal, desktop, or claude.ai/code on the web) and Codex, in a repo | `greenlight setup --project` in the repo, then commit the three files it writes | the checkout the agent works in |
| Claude Code or Codex, in every project on your machine | `greenlight setup` (Claude Code), `greenlight setup --codex` (Codex CLI, IDE and the ChatGPT desktop app) | the checkout the agent works in |
| Claude Desktop chat | `greenlight setup --claude-desktop` | GitHub, no clone |
| claude.ai or ChatGPT, on the web or your phone | run `greenlight serve` (or the container) where they can reach it, then add its URL as a connector | GitHub, no clone |
| Codex cloud tasks | no MCP servers there yet: install the CLI in the environment's setup script and use `greenlight run` | the task's checkout |

### Claude Code and Codex in a repo

In the repo:

```bash
greenlight setup --project
git add .mcp.json .claude/settings.json .codex/config.toml
git commit -m "Start greenlight in Claude Code and Codex sessions"
```

That writes the repo's own MCP config: `.mcp.json` for Claude Code, `.codex/config.toml` for Codex, and `.claude/settings.json` approving the server so nobody gets asked. Each starts greenlight through `uvx`, which downloads it on first use, so nobody installs anything by hand. Anyone with [uv](https://docs.astral.sh/uv/) gets it, and Claude Code on the web already has uv.

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

These connect to MCP servers by URL from their own servers, so greenlight has to run somewhere on the internet with HTTPS. It reads the repo from GitHub and refreshes it every 10 minutes, so wherever it runs needs no clone.

1. Run it. With Docker on macOS or Linux:

   ```bash
   docker run -d --name greenlight -p 8000:8000 -v greenlight-data:/data \
     -e GREENLIGHT_REPO=rathojohn/greenlight \
     -e GREENLIGHT_MCP_TOKEN=$(openssl rand -hex 16) \
     ghcr.io/rathojohn/greenlight
   docker logs greenlight
   ```

   Windows PowerShell:

   ```powershell
   docker run -d --name greenlight -p 8000:8000 -v greenlight-data:/data `
     -e GREENLIGHT_REPO=rathojohn/greenlight `
     -e GREENLIGHT_MCP_TOKEN=$([guid]::NewGuid().ToString("N")) `
     ghcr.io/rathojohn/greenlight
   docker logs greenlight
   ```

   Use your repo instead of `rathojohn/greenlight`. For a private one, also pass a token: add `-e GITHUB_TOKEN=$(gh auth token)` (the same in PowerShell). `docker logs` prints the path to connect to, like `http://localhost:8000/9f2c4e.../mcp`. Without Docker, `greenlight serve --repo rathojohn/greenlight --host 0.0.0.0` does the same.

2. Give it an HTTPS address. To try it from your own machine, a Cloudflare quick tunnel is free and needs no account: `cloudflared tunnel --url http://localhost:8000` prints an `https://...trycloudflare.com` address. It changes every run, and your machine has to stay on. To keep it up, run the same container on any host that runs containers; it listens on `$PORT` when the host sets one.

3. Add the connector. The URL is the address from step 2 plus the path from `docker logs`: with `https://abc.trycloudflare.com` and `/9f2c4e.../mcp`, it's `https://abc.trycloudflare.com/9f2c4e.../mcp`.
   - claude.ai: Settings, Connectors, Add custom connector. It shows up in Claude Desktop too. Free plans get one custom connector.
   - ChatGPT: turn on Developer mode (Settings, Security and login), then add it at chatgpt.com/plugins with the plus button. Developer mode is on paid plans, and ChatGPT asks before running a tool that changes something.

The token in that URL is the password: anyone with the URL can read the repo's test and CI history through it, quarantine tests, and, if the server's GitHub token can write issues, open flaky-test issues. Start the container with a new token to revoke it. Clients that can send headers (Claude Code with `--header`, Codex with `bearer_token_env_var`) can use `/mcp` with `Authorization: Bearer <token>` instead. `--no-auth` turns the token off, which only makes sense for a public repo.

### Codex cloud and other cloud agents

Codex cloud doesn't start MCP servers yet. Install the CLI in the environment's setup script and let the agent rule below do the rest:

```bash
uv tool install "greenlight @ git+https://github.com/rathojohn/greenlight"
```

### What agents should do

This is the rule `setup` prints (and `--agent-rules` writes):

```
## Test failures (greenlight)
Record every test run with greenlight, passing or not (passes are the history that tells a flaky test
from a broken one), and on a failure follow its decision before rerunning anything. In a shell:
`greenlight run -- <test command>`. With the MCP server: call greenlight_gate_junit with the run's
JUnit report (or greenlight_playtest_gate for a playtest ledger).
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
| greenlight_query | no | read-only SQL over every table |

Read from GitHub (Claude Desktop with `--repo`, claude.ai and ChatGPT connectors, the container), there's no checkout of yours, so the two tools that record a local test run (`greenlight_gate_junit`, `greenlight_playtest_gate`) aren't offered.

## Run it in a container

The image is `ghcr.io/rathojohn/greenlight`, for amd64 and arm64 (Apple Silicon too), built from this repo's `Dockerfile` on every push to main. CI starts it and checks the MCP server against GitHub before publishing.

```bash
docker run --rm ghcr.io/rathojohn/greenlight --version
```

- With no command it serves MCP over HTTP: see [claude.ai and ChatGPT](#claudeai-and-chatgpt-web-and-phone).
- Any CLI command works in place of that, like `--version` above, or `mcp --repo <owner/name>` for the stdio server (run with `docker run -i`, as in the Claude Desktop example below).
- Cache clones and the database live in `/data`. Mount a volume there (`-v greenlight-data:/data`) to keep them across restarts.

Claude Desktop can run it too, in its config file:

```json
{
  "mcpServers": {
    "greenlight": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "greenlight-data:/data", "ghcr.io/rathojohn/greenlight",
               "mcp", "--repo", "rathojohn/greenlight"]
    }
  }
}
```

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

| Backend | Setup |
| --- | --- |
| Local Grafana (Tempo, Prometheus, Loki) | `docker compose -f deploy/otel/docker-compose.yml up -d`, then open http://localhost:3000. Keeps what your disk keeps. |
| Grafana Cloud | endpoint from your stack's OTLP page; `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20<base64 of instance:token>"` |
| Honeycomb | endpoint `https://api.honeycomb.io`, `OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=<key>"` |
| Datadog | an Agent with OTLP ingest enabled on 4318. The spans arrive as ordinary APM traces. |
| Anything else | an OpenTelemetry Collector in front of it |

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
| MCP server over HTTP | a token in the URL path (`/<token>/mcp`) or an `Authorization: Bearer` header | `$GREENLIGHT_MCP_TOKEN`; never logged |
| Clones greenlight keeps itself | the GitHub token above, handed to git in its environment for each call | never written to disk |

`greenlight auth` prints where the token came from (never the token), the access it has and the rate limit left. Public repos sync without a token at 60 requests an hour.

A fine-grained token scoped to just the repo needs:

| Command | Permissions |
| --- | --- |
| `sync` | Metadata, Contents, Pull requests, Issues, Actions, Deployments: read |
| `issues --apply` | Issues: read and write |
| `ci report --pr-comment` | Pull requests: read and write |
| `ci record` to the data branch | Contents: read and write |

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
| `records` | run records on the `greenlight-data` branch, written by the Action | a clone |
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
  contents: write        # run records on the greenlight-data branch
  pull-requests: write   # one comment per job on the PR
  issues: read           # quarantine labels

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
```

The rerun lands as a second attempt on the same commit, so a pass there is a recorded flip and the second report (and the PR comment) says PASS.

Actions jobs start with an empty disk, so the history lives in your repo: each job restores every record from the `greenlight-data` branch into a fresh DB, adds its own run, gates, and pushes its record back (records have unique names, so parallel jobs never conflict). It writes the verdict to the job summary and edits a single comment on the pull request. `fail-on` decides what fails the job: `real` (default, only REAL_FAILURE), `any`, or `never`. A matrix entry counts as its own environment, so a test that always fails on one Python and passes on another isn't called flaky.

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

- **Overview**: the latest verdict, rerun share, time spent rerunning, and the most unstable tests across recent commits (taller bars failed more often; amber means the same commit passed and failed).
- **Flaky tests**, **test pages** (results by commit, duration, failure messages, measured numbers against a baseline, linked issues, quarantine controls), **Runs** and **run pages** (why each failure did or didn't block).
- **Pipelines**: runs per day, per-workflow success rate, p50/p95 duration and queue time, flaky and slow jobs, and a job waterfall per run.
- **Delivery**: the four DORA numbers, the deployments behind them, pull request flow and the issue backlog.
- **Trends**: forecasts from the Toto 2.0 time series model (optional, `pip install "greenlight[toto]"`, Python 3.12+): rerun churn, failure rate and suite duration with a 7-day band, and tests running slower than forecast.
- **Quarantine**: suggestions, what's quarantined, what's clean enough to release.

## Configuration

`greenlight.toml` is looked up in this folder or a parent (for the MCP server, the project the agent is working in), then `~/.greenlight/config.toml`; `--config` and `$GREENLIGHT_CONFIG` override. Every key is optional. `greenlight setup` (or just `greenlight init`) writes one.

```toml
db = "~/.greenlight/your-repo.db"

[github]
repo = "owner/name"              # default: parsed from the clone's origin

[git]
path = "."                       # the local clone: data branch, release notes, lead time

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

[ci]
data_branch = "greenlight-data"

[run]
junit = "reports/*.xml"          # what `greenlight run` records (default: any JUnit XML the tests write)

[playtest]                       # a test ledger committed to git, see below
enabled = false
```

Read from GitHub (`--repo`), greenlight uses a `greenlight.toml` committed on the default branch if there is one, and otherwise guesses what marks a release the way `init` does: release notes, then a `release` branch, then GitHub Releases.

Environment variables:

| Variable | What |
| --- | --- |
| `GREENLIGHT_REPO` | `owner/name` for `greenlight mcp` and `greenlight serve` to read from GitHub |
| `GREENLIGHT_MCP_TOKEN` | the HTTP server's token |
| `GREENLIGHT_HOME` | where greenlight keeps its clones, databases and `config.toml` (default `~/.greenlight`; `/data` in the container) |
| `GREENLIGHT_DB` | one database file, overriding everything else |
| `GREENLIGHT_CONFIG` | one `greenlight.toml`, overriding the lookup |
| `GREENLIGHT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | GitHub token (see [How auth works](#how-auth-works)) |
| `GREENLIGHT_GIT_BASE` | where repos are cloned from, for GitHub Enterprise (default `https://github.com`) |
| `PORT` | the HTTP server's port, when a host sets it |

### Test ledgers in git

Some projects run tests outside CI (in coding-agent sessions, on a laptop) and commit a small record of each run instead. `[playtest]` reads one such format, `tools/playtest/runs/*.json`, from every branch: each record names the commit, the uncommitted files it tested (by content hash), and per suite which checks failed or ran slower than a baseline. Records name only failures, so greenlight infers that a check passed when its suite passed, which is exact on the same code. `greenlight playtest gate` syncs and gates the run just made, and prints the command that reruns only the flaky suites. `integrations/survive-project/` is a worked example. Other ledger formats are a small adapter away (`greenlight/playtest.py` is about 300 lines).

## Limits

- Polling, not streaming: data is as fresh as the last `sync`.
- Read from GitHub (Claude Desktop, connectors, the container), data is up to 10 minutes old, and a fresh server's first answer waits on its first sync (a few seconds for a small repo; a busy one can take a minute, and the tool says to try again).
- Test runs recorded with `greenlight run` stay in the database of the machine that ran them. CI runs are shared through the data branch, and playtest ledgers through git.
- The repo is the backup. The data branch grows by a few KB per CI job.
- The OTel CI/CD conventions are still in development upstream, so attribute names may change in later releases.
- One repo per DB. Point `db` at a different file per project.
- `greenlight otel receive` takes JSON only (set `encoding: json` on the Collector's otlphttp exporter); protobuf would need a dependency the CLI otherwise avoids.
- Toto forecasts need about two weeks of daily data before they mean anything.
- No live view of runs in progress, and no log search.
- New tests aren't run several times up front to shake out flakes; a new test that fails just blocks.
- Change failure rate counts incidents from issue labels, not an incident tool.
- Not built: test impact analysis, code coverage, per-test ownership, alerting. For alerts, alert on the exported metrics in your OTel backend.

## License

MIT
