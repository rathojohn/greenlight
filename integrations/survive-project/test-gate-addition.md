## When a suite fails: flaky or real (greenlight)

Add under "Every time" in `.claude/skills/test-gate/SKILL.md`, between steps 5 and 6. Adjust it to the
rules there; it does not change what a branch is responsible for.

5b. If the run printed a failure, run `greenlight playtest gate`. It reads every record on every
    fetched branch (fetch first, as in step 1), then judges each failed check by its history on the
    same code: a check that both failed and passed on the same commit before is flaky.
    - `PASS`: nothing failed that counts.
    - `RERUN_TARGETED`: only checks with a flake history failed. Run the command it prints once
      (`node tools/playtest/run.cjs <suites> --rerun`). That is the one flake confirmation the rerun
      rules allow. Commit both records; the rerun is what proves the flake.
    - `REAL_FAILURE`: the check has no flake history. It's real: handle it the way "Test gate rules"
      and the merge rules say (the branch's own suites only).
    Name the verdict and any flaky-test issue it mentions in the handoff.
