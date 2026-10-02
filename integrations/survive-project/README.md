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

1. Copy `greenlight.toml` to the repo root.
2. Add `session-start.sh` to `.claude/hooks/session-start.sh` so every cloud session has the CLI.
3. Add `test-gate-addition.md` to the test-gate skill, so sessions ask greenlight before rerunning.
4. On your machine: `greenlight sync` then `greenlight ui` for the dashboard. With `[otel] endpoint`
   set, `sync` also sends every run to your OTel backend.
5. Perf regressions to issues (docs/TESTING.md, "Perf regressions"): `greenlight issues` shows what it
   would open; `greenlight issues --apply` opens them. It links issue #44 to its check instead of
   duplicating it.

## Optional: numbers in the records

Records name which checks failed or ran slower, not how long suites took or the perf medians.
`ledger-numbers.patch` adds both (a suite's `secs`, and each measured check's `[value, base]`), so the
test pages chart perf medians against their base build over time. It is a few lines in
`tools/playtest/ledger.cjs`, and `tools/playtest/` is the playtest runner's own rule (studio only).
