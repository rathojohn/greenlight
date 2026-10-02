# greenlight

Local flaky-test history in SQLite, a triage gate that decides whether a red run actually needs a regression, a local dashboard, an MCP server so Claude Code and Codex can query it, and Toto 2.0 forecasting for duration and rerun trends.

No hosted platform, no retention limits. The DB is one file.

## Install (Windows, PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[toto]"     # drop [toto] if you only want flake tracking
$env:GREENLIGHT_DB = "$HOME\.greenlight\greenlight.db"   # optional, this is the default
```

Toto 2.0 needs Python 3.12+. The default checkpoint is `Datadog/Toto-2.0-22m`, which downloads once (small) and runs on CPU in well under a second for a few hundred series. Set `GREENLIGHT_TOTO_MODEL` to try a bigger one.

Try it on fake data first:

```powershell
python examples\seed_demo.py --db demo.db
greenlight --db demo.db flaky
greenlight --db demo.db gate
greenlight --db demo.db trends
greenlight --db demo.db ui
```

## Dashboard

```powershell
greenlight ui                           # http://127.0.0.1:8765, opens your browser
greenlight ui --export snapshot.html    # read-only, self-contained file you can share
```

- **Overview** leads with the latest gate decision, then rerun share, time spent rerunning, and a grid of the most unstable tests across recent commits. Height means a failure, amber means the same commit both passed and failed.
- **Flaky tests** ranks everything that flipped, with a filter and each test's last 30 results.
- **Test** pages show results by commit, daily duration with a Toto forecast band, failure messages grouped by signature, and quarantine controls.
- **Runs** lists the last 50 runs with the gate decision for each. A run page explains why each failure was or wasn't blocking.
- **Trends** is the Toto view: rerun churn, failure rate, suite duration and run volume with a 7-day forecast, plus tests running slower than forecast.
- **Quarantine** shows suggestions from the sweep, what's quarantined, and what's been clean long enough to release.

The server binds to 127.0.0.1 only, rejects requests with a foreign Host header, and only accepts writes that carry a custom header, so other sites open in your browser can't read it or quarantine things. The 7d/30d/90d switch applies to Overview, Flaky tests, test pages and Quarantine.

## Wire it into every test run

1. Have the runner write JUnit XML. pytest: `--junitxml=reports/junit.xml`. vitest: `--reporter=junit --outputFile=reports/junit.xml`. jest: `jest-junit`. Playwright: `reporter: [['junit', { outputFile: 'reports/junit.xml' }]]`. Go: `go-junit-report`.
2. After the run, record it and gate on it:

```powershell
$sha = git rev-parse HEAD
greenlight ingest reports\*.xml --sha $sha --branch (git branch --show-current) --source codex
greenlight gate --sha $sha
```

Gate exit codes:

| Code | Decision | What to do |
| --- | --- | --- |
| 0 | PASS | Nothing blocking. Quarantined or retried-green failures are ignored. |
| 2 | RERUN_TARGETED | Only known flaky tests failed. Rerun just the tests it prints, not the suite. |
| 1 | REAL_FAILURE | A stable or brand-new test failed. This is the only case that justifies a full regression. |
| 3 | error | Bad input, missing DB, etc. |

Every run you ingest becomes history. Reruns of the same SHA get `attempt` 2, 3... automatically, and that is what makes flake detection work. Retries inside a run (pytest-rerunfailures, Playwright/Jest retries, Surefire `flakyFailure`) are picked up too.

## Tell the agents

Paste into `CLAUDE.md` and `AGENTS.md`:

```
## Test failures
After any test run, run `greenlight ingest` then `greenlight gate` (or call greenlight_triage_run).
- PASS: continue.
- RERUN_TARGETED: rerun only the listed tests, once. Do not start a full regression.
- REAL_FAILURE: investigate the blocking tests. Full regression only after a fix.
Never quarantine a test without telling me why. Never unquarantine on your own.
```

## Register the MCP server

Claude Code:

```powershell
claude mcp add greenlight -e GREENLIGHT_DB=$HOME\.greenlight\greenlight.db -- C:\path\to\.venv\Scripts\python.exe -m greenlight.server
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.greenlight]
command = "C:\\path\\to\\.venv\\Scripts\\python.exe"
args = ["-m", "greenlight.server"]
env = { GREENLIGHT_DB = "C:\\Users\\you\\.greenlight\\greenlight.db" }
```

Tools:

| Tool | Writes | Purpose |
| --- | --- | --- |
| greenlight_triage_run | no | Same decision as `gate`, with per-failure detail |
| greenlight_list_flaky | no | Ranked flaky tests |
| greenlight_test_history | no | One test's recent runs, stats, quarantine status |
| greenlight_quarantine / _unquarantine | yes | Manage quarantine |
| greenlight_sweep | only with apply=true | Quarantine candidates, plus quarantined tests clean enough to release |
| greenlight_duration_regressions | no | Toto: tests whose recent durations broke above forecast |
| greenlight_suite_forecast | no | Toto: forecast failure_rate, suite_duration_ms, runs, or reruns |
| greenlight_query | no | Read-only SQL over runs, results, quarantine |

## How the decision works

- A **flip** is a commit where a test both passed and failed. Same code, different result, so it is nondeterministic by definition. Only commits where the test ran 2+ times can flip, and those are the denominator.
- **flaky** = flipped on 2+ commits in the window (default 30 days). **suspect** = 1 commit. Ranking uses the Wilson lower bound so 5 of 10 outranks 1 of 1.
- A failing test in triage is:
  - `quarantined`: ignored.
  - `new_test`: first time this test has ever run. Blocks.
  - `known_flaky` / `suspect_flaky`: rerun only.
  - `real_failure`: no flake history. Blocks. A flaky test that has failed 3+ times on this commit without a single pass is also promoted to `real_failure`, so a flaky label cannot hide a real break.
- `new_signature: true` means a flaky test failed with a message it has not produced before. It still reruns, but it is worth a look.
- `sweep` never auto-releases quarantine. It lists tests with 10+ clean runs in 14 days and you decide.

## Where Toto fits

Flake detection is counting, so it does not use a model. Toto handles the continuous signals:

- **Duration regressions.** Each test's daily median passing duration is forecast from its history, holding out the last 3 days. A day above p90 and at least 20% above p50 gets flagged. Slowdowns tend to precede timeout flakes.
- **Suite trends.** `reruns` is the one to watch after you adopt the gate. If the churn is really dropping, that series shows it.

Forecasts need about 2 weeks of daily data before they mean anything. Days with no runs are treated as missing, not zero, except for run counts.

## If tests also run in GitHub Actions

The DB is local, so hosted CI cannot reach it. Two options: upload the JUnit file as an artifact and pull it down with `gh run download`, then ingest with `--external-id <run id>` (re-ingesting the same id is a no-op), or move the DB to a free hosted Postgres. The SQL is mostly portable, but the second option is real work.
