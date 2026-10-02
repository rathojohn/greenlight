# greenlight

CI/CD observability on OpenTelemetry, for when you can't (or won't) pay for a platform.

greenlight answers one question after every red test run: is this real, or is it the same flaky test again? Around that it tracks flaky tests, CI pipelines, deployments and DORA metrics from git and GitHub, sends all of it to any OpenTelemetry backend as traces and metrics, and gives you a local dashboard and an MCP server so Claude Code or Codex can ask it before rerunning anything.

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [OpenTelemetry](#opentelemetry)
- [How auth works](#how-auth-works)
- [GitHub: sync, issues and CI](#github-sync-issues-and-ci)
- [The gate](#the-gate)
- [Dashboard](#dashboard)
- [MCP server for coding agents](#mcp-server-for-coding-agents)
- [Datadog feature map](#datadog-feature-map)
- [Configuration](#configuration)
- [Limits](#limits)

## How it works

```
 sources                         greenlight                          out
 ───────                         ──────────                          ───
 JUnit XML (any runner)   ─┐                                  ┌─> OTLP traces + metrics
 OTLP test / CI spans     ─┤     SQLite index                 │   (Grafana, Tempo, Jaeger,
 GitHub API: Actions,     ─┼──>  gate: PASS / RERUN / REAL ───┤    Honeycomb, Datadog, ...)
   PRs, issues, deploys    │     flake stats, quarantine      ├─> local dashboard
 git: run records on a    ─┤     pipelines, DORA              ├─> MCP server (agents)
   data branch, release    │                                  ├─> GitHub issues per flaky test
   notes, a test ledger   ─┘                                  └─> PR comment + CI step summary
```

The core idea: **a flaky test is one that both passed and failed on the same code.** That's counting, not machine learning. Every rerun of the same commit is evidence, so greenlight keeps every run and decides what a new failure means from that history.

## Quick start

Needs Python 3.11 or newer. The `python3` that ships with macOS is 3.9, which is too old (pip then fails with "No matching distribution found for mcp"). [uv](https://docs.astral.sh/uv/) fetches a current Python for you.

macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv venv -p 3.12 .venv
source .venv/bin/activate
uv pip install "greenlight @ git+https://github.com/rathojohn/greenlight"
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install "greenlight @ git+https://github.com/rathojohn/greenlight"
```

If you already have Python 3.11 or newer, a plain venv and `pip install` work too. The CLI and dashboard use only the standard library; the MCP server needs `mcp`, which installs with it.

Try it on fake data first. `greenlight demo` writes 60 days of runs, pipelines, releases, pull requests and issues into a separate file:

```bash
greenlight demo --db demo.db
greenlight --db demo.db gate
greenlight --db demo.db flaky
greenlight --db demo.db ui
```

`gate` reports REAL_FAILURE (one stable test broke on the last commit) and exits 1. `ui` opens the dashboard at http://127.0.0.1:8765.

On a real repo:

```bash
cd your-repo
greenlight init
greenlight auth
greenlight sync
greenlight ui
```

`init` writes a `greenlight.toml` with what it can guess, `auth` shows where the GitHub token comes from and what it can do, and `sync` pulls runs, PRs, issues, Actions and deployments (and exports to OTel if an endpoint is set).

After a local test run, record it and gate on it:

```bash
greenlight ingest reports/*.xml --sha $(git rev-parse HEAD) --branch $(git branch --show-current)
greenlight gate --sha $(git rev-parse HEAD)
```

`gate` exits 0 for PASS, 2 for RERUN_TARGETED, 1 for REAL_FAILURE and 3 for an error.

Any runner that writes JUnit XML works: pytest `--junitxml`, vitest `--reporter=junit`, jest-junit, Playwright's junit reporter, go-junit-report, Surefire.

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

| Backend | Cost | Setup |
| --- | --- | --- |
| Local Grafana (Tempo, Prometheus, Loki) | free, on your machine | `docker compose -f deploy/otel/docker-compose.yml up -d`, then open http://localhost:3000. Keeps what your disk keeps. |
| Grafana Cloud | free tier (traces and logs kept 14 days) | endpoint from your stack's OTLP page; `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20<base64 of instance:token>"` |
| Honeycomb | free tier | endpoint `https://api.honeycomb.io`, `OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=<key>"` |
| Datadog | paid | an Agent with OTLP ingest enabled on 4318. The spans arrive as ordinary APM traces. |
| Anything else | | an OpenTelemetry Collector in front of it |

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
| MCP server | none: it's a local stdio process your agent starts | |

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

That's Datadog-style auto test retries without the agent: the rerun lands as a second attempt on the same commit, so a pass there is a recorded flip and the second report (and the PR comment) says PASS.

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

Tell your agents, in `CLAUDE.md` and `AGENTS.md`:

```
## Test failures
After any test run, record it (`greenlight ingest`) and run `greenlight gate`, or call greenlight_triage_run.
- PASS: continue.
- RERUN_TARGETED: rerun only the listed tests, once. No full regression.
- REAL_FAILURE: investigate the blocking tests. Full regression only after a fix.
Never quarantine a test or apply issue changes without telling me why.
```

## Dashboard

`greenlight ui` serves it on http://127.0.0.1:8765; `greenlight ui --export snapshot.html` writes a read-only, self-contained copy you can share.

- **Overview**: the latest verdict, rerun share, time spent rerunning, and the most unstable tests across recent commits (taller bars failed more often; amber means the same commit passed and failed).
- **Flaky tests**, **test pages** (results by commit, duration, failure messages, measured numbers against a baseline, linked issues, quarantine controls), **Runs** and **run pages** (why each failure did or didn't block).
- **Pipelines**: runs per day, per-workflow success rate, p50/p95 duration and queue time, flaky and slow jobs, and a job waterfall per run.
- **Delivery**: the four DORA numbers, the deployments behind them, pull request flow and the issue backlog.
- **Trends**: forecasts from [Toto 2.0](https://github.com/DataDog/toto) (optional, `pip install "greenlight[toto]"`, Python 3.12+): rerun churn, failure rate and suite duration with a 7-day band, and tests running slower than forecast.
- **Quarantine**: suggestions, what's quarantined, what's clean enough to release.

## MCP server for coding agents

```bash
claude mcp add greenlight -e GREENLIGHT_DB=$HOME/.greenlight/your-repo.db -- /path/to/.venv/bin/python -m greenlight.server
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.greenlight]
command = "/path/to/.venv/bin/python"
args = ["-m", "greenlight.server"]
env = { GREENLIGHT_DB = "/home/you/.greenlight/your-repo.db" }
```

Started in a repo with a `greenlight.toml`, the server picks up that config.

| Tool | Writes | Purpose |
| --- | --- | --- |
| greenlight_triage_run | no | the gate's decision with per-failure detail |
| greenlight_playtest_gate | local DB | sync a test ledger in git, then gate the run just made |
| greenlight_list_flaky, greenlight_test_history | no | ranked flaky tests; one test's history |
| greenlight_quarantine / _unquarantine, greenlight_sweep | local DB | manage quarantine |
| greenlight_pipelines | no | workflow health, flaky and slow jobs |
| greenlight_delivery | no | DORA, PR flow, issue backlog |
| greenlight_issues | GitHub, only with apply=true | plan or apply the flaky-test and perf issues |
| greenlight_sync | local DB, reads GitHub | pull everything configured |
| greenlight_duration_regressions, greenlight_suite_forecast | no | Toto forecasts |
| greenlight_query | no | read-only SQL over every table |

## Datadog feature map

Built against what Datadog's CI/CD products do, with the gaps stated plainly.

| Datadog | greenlight | Gap |
| --- | --- | --- |
| CI Pipeline Visibility | Pipelines view; every attempt, job and step as OTel traces | polls after the fact (or the Collector's webhook receiver); no live in-progress view; no log search |
| Test Optimization: flaky test management | the gate, flake scores, quarantine, one issue per flaky test, quarantine by label | no per-test ownership |
| Auto test retries | the Action's `rerun-names` output and a second record step; retried passes count as flips | you wire the rerun step |
| Early flake detection | `new_test` blocks, `new_signature` flags | new tests aren't run several times automatically |
| Test impact analysis | not built | |
| Quality gates | `gate` and the Action's `fail-on` | one rule set, not configurable per repo |
| DORA metrics | Delivery view and `greenlight.dora.*` metrics | incidents come from issue labels, not an incident tool |
| Code coverage | not built | |
| Monitors and alerts | not built here | alert on the exported metrics in your backend |
| Bits AI | MCP server for Claude Code and Codex | |

If you need several of the gaps, the honest answer is a paid tool. For one person or a small team, this covers the part that wastes the most time: rerunning everything because one test is flaky.

## Configuration

`greenlight.toml` is looked up in this folder or a parent, then `~/.greenlight/config.toml`; `--config` and `$GREENLIGHT_CONFIG` override. Every key is optional. `greenlight init` writes one.

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

[playtest]                       # a test ledger committed to git, see below
enabled = false
```

### Test ledgers in git

Some projects run tests outside CI (in coding-agent sessions, on a laptop) and commit a small record of each run instead. `[playtest]` reads one such format, `tools/playtest/runs/*.json`, from every branch: each record names the commit, the uncommitted files it tested (by content hash), and per suite which checks failed or ran slower than a baseline. Records name only failures, so greenlight infers that a check passed when its suite passed, which is exact on the same code. `greenlight playtest gate` syncs and gates the run just made, and prints the command that reruns only the flaky suites. `integrations/survive-project/` is a worked example. Other ledger formats are a small adapter away (`greenlight/playtest.py` is about 300 lines).

## Limits

- Polling, not streaming: data is as fresh as the last `sync`.
- The repo is the backup. The data branch grows by a few KB per CI job.
- The OTel CI/CD conventions are still in development upstream, so attribute names may change in later releases.
- One repo per DB. Point `db` at a different file per project.
- `greenlight otel receive` takes JSON only (set `encoding: json` on the Collector's otlphttp exporter); protobuf would need a dependency the CLI otherwise avoids.
- Toto forecasts need about two weeks of daily data before they mean anything.

## License

MIT
