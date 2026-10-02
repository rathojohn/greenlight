# greenlight on survive-project

A worked example of greenlight on a repo whose tests don't run in CI. survive-project's playtests run
in Claude Code sessions, and each run commits a record to `tools/playtest/runs/` on its branch
(`tools/playtest/ledger.cjs`). Those records are the history greenlight needs; nothing else has to
store anything.

What it found on the first sync (66 records across every branch, 2026-10-02):

- `smoke: the Lantern points at the foe it locked on` failed on 6 commits, and on both commits that
  were rerun it failed and then passed. That's flaky by greenlight's rule, so the gate reruns it
  instead of calling it a real failure.
- Two more checks flipped once (`studio: the frame on screen matches the clock`, `treegame: the gem
  left behind fades out once you step 4 m away`): suspects.
- 33 shipped builds (patch notes files) in 5 days, with a median lead time from commit to ship of
  about 7.5 hours.

## Setup

What survive-project has, all committed:

1. `.mcp.json`, `.claude/settings.json` and `.codex/config.toml` from `greenlight setup --project`. Every
   Claude Code session on the repo, cloud sessions included, starts greenlight's MCP server through `uvx`
   and gets its tools (`greenlight_overview`, `greenlight_playtest_gate` and the rest). Checked in a fresh
   cloud session: the server synced on first use and answered with the Lantern check as flaky.
2. `session-start.sh` as `.claude/hooks/session-start.sh`, so the CLI is there too (`greenlight playtest
   gate`). It can't provide the MCP server: Claude Code starts project MCP servers before hooks run.
3. `test-gate-addition.md` in the test-gate skill, so sessions ask greenlight before rerunning.

No `greenlight.toml` is needed (the repo ignores it): with none, greenlight finds the playtest ledger and
counts each patch notes file added on main as a release. `greenlight.toml` here is the same thing written
out, for a machine that wants to change it.

Elsewhere, with no clone:

- Claude Desktop: `greenlight setup --claude-desktop --repo rathojohn/survive-project`.
- claude.ai or ChatGPT: run the container with `GREENLIGHT_REPO=rathojohn/survive-project` and a
  `GITHUB_TOKEN` that can read the repo (it's private), then add its URL as a connector (see the main
  README).
- Perf regressions to issues (docs/TESTING.md, "Perf regressions"): `greenlight issues` shows what it
  would open; `greenlight issues --apply` opens them. It links issue #44 to its check instead of
  duplicating it.

A cloud session's clone carries limited history, so release numbers from there cover recent commits
(31 releases and a 10.9 h median lead time in that check, against 33 and 7.5 h from a full clone).

## Optional: numbers in the records

Records name which checks failed or ran slower, not how long suites took or the perf medians.
`ledger-numbers.patch` adds both (a suite's `secs`, and each measured check's `[value, base]`), so the
test pages chart perf medians against their base build over time. It is a few lines in
`tools/playtest/ledger.cjs`, and `tools/playtest/` is the playtest runner's own rule (studio only).
