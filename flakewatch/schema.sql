-- flakewatch schema (SQLite). Safe to run repeatedly.
PRAGMA journal_mode = WAL;

-- One row per test-suite execution. Re-running the same SHA creates a new row
-- with a higher attempt number, which is what makes flake detection possible.
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT UNIQUE,                 -- CI run id etc. Makes ingest idempotent.
    commit_sha   TEXT NOT NULL,
    branch       TEXT,
    attempt      INTEGER NOT NULL DEFAULT 1,  -- 1 = first run on this SHA, 2+ = rerun
    source       TEXT,                        -- 'ci', 'local', 'codex', 'claude-code'...
    started_at   TEXT NOT NULL,               -- ISO 8601 UTC
    duration_ms  INTEGER
);

-- One row per test execution. In-run retries (pytest-rerunfailures, jest/playwright
-- retries, surefire flakyFailure) get their own row with retry = 0, 1, 2...
CREATE TABLE IF NOT EXISTS results (
    run_id       INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    test_id      TEXT NOT NULL,               -- "classname::name"
    file         TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN ('pass', 'fail', 'error', 'skip')),
    duration_ms  INTEGER,
    retry        INTEGER NOT NULL DEFAULT 0,
    failure_sig  TEXT,                        -- hash of the normalized failure message
    message      TEXT,                        -- first 500 chars of the failure
    PRIMARY KEY (run_id, test_id, retry)
);

CREATE INDEX IF NOT EXISTS idx_results_test  ON results(test_id);
CREATE INDEX IF NOT EXISTS idx_runs_sha      ON runs(commit_sha);
CREATE INDEX IF NOT EXISTS idx_runs_started  ON runs(started_at);

-- Quarantined tests still run and still get recorded, they just stop blocking.
CREATE TABLE IF NOT EXISTS quarantine (
    test_id   TEXT PRIMARY KEY,
    reason    TEXT,
    added_at  TEXT NOT NULL,
    added_by  TEXT NOT NULL DEFAULT 'manual'   -- 'manual' or 'auto'
);
