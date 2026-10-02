-- greenlight schema (SQLite). Safe to run repeatedly. Columns added after a table first shipped
-- live in db.py's MIGRATIONS too, so older databases pick them up.
PRAGMA journal_mode = WAL;

-- One row per test-suite execution. Re-running the same SHA creates a new row
-- with a higher attempt number, which is what makes flake detection possible.
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT UNIQUE,                 -- CI run id etc. Makes ingest idempotent.
    commit_sha   TEXT NOT NULL,               -- identity of the code tested; "<sha>+<hash>" when it had local changes
    branch       TEXT,
    attempt      INTEGER NOT NULL DEFAULT 1,  -- 1 = first run on this SHA, 2+ = rerun
    source       TEXT,                        -- 'ci', 'local', 'codex', 'claude-code', 'playtest'...
    started_at   TEXT NOT NULL,               -- ISO 8601 UTC
    duration_ms  INTEGER,
    git_commit   TEXT,                        -- the plain commit when commit_sha carries local changes
    total_tests  INTEGER,                     -- set when the source names fewer results than it ran
    session      TEXT,                        -- agent session or CI run that produced it
    command      TEXT,
    url          TEXT
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
    flags        TEXT,                        -- comma list: inferred, slower, known
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
    added_by  TEXT NOT NULL DEFAULT 'manual'   -- 'manual', 'auto', 'github#<issue>', 'known-failures.json'
);

-- Numbers measured by a test, e.g. a benchmark's median against its base build. test_id can
-- also be a suite name for suite-level numbers (its duration).
CREATE TABLE IF NOT EXISTS metrics (
    run_id   INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    test_id  TEXT NOT NULL,
    name     TEXT NOT NULL,                   -- value_ms, base_ms, budget_ms, duration_ms
    value    REAL NOT NULL,
    PRIMARY KEY (run_id, test_id, name)
);
CREATE INDEX IF NOT EXISTS idx_metrics_test ON metrics(test_id, name);

-- CI pipelines: one row per GitHub Actions workflow run attempt.
CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id  TEXT PRIMARY KEY,            -- "gha:<run id>:<attempt>"
    provider     TEXT NOT NULL,
    workflow     TEXT NOT NULL,
    run_number   INTEGER,
    attempt      INTEGER NOT NULL DEFAULT 1,
    event        TEXT,
    branch       TEXT,
    commit_sha   TEXT,
    status       TEXT NOT NULL,               -- success, failure, cancelled, skipped, timed_out, in_progress...
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER,
    queue_ms     INTEGER,
    actor        TEXT,
    url          TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipelines_created ON pipelines(created_at);
CREATE INDEX IF NOT EXISTS idx_pipelines_sha ON pipelines(commit_sha);

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,            -- "gha:<job id>"
    pipeline_id  TEXT NOT NULL REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER,
    queue_ms     INTEGER,
    runner       TEXT,
    url          TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_pipeline ON jobs(pipeline_id);

CREATE TABLE IF NOT EXISTS steps (
    job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    number       INTEGER NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    duration_ms  INTEGER,
    started_at   TEXT,
    PRIMARY KEY (job_id, number)
);

-- Deployments, for DORA metrics: pushes to a release branch, GitHub deployments or releases,
-- or files added in git (patch notes).
CREATE TABLE IF NOT EXISTS deployments (
    deploy_id     TEXT PRIMARY KEY,
    source        TEXT NOT NULL,              -- branch-push, github-deployment, github-release, git-file
    environment   TEXT NOT NULL,
    commit_sha    TEXT NOT NULL,
    previous_sha  TEXT,
    deployed_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'success',
    version       TEXT,
    url           TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_at ON deployments(deployed_at);

-- The commits each deployment shipped, for lead time for changes.
CREATE TABLE IF NOT EXISTS deploy_commits (
    deploy_id     TEXT NOT NULL REFERENCES deployments(deploy_id) ON DELETE CASCADE,
    commit_sha    TEXT NOT NULL,
    authored_at   TEXT NOT NULL,
    PRIMARY KEY (deploy_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    number        INTEGER PRIMARY KEY,
    title         TEXT,
    author        TEXT,
    state         TEXT NOT NULL,              -- open, merged, closed
    draft         INTEGER NOT NULL DEFAULT 0,
    base          TEXT,
    head          TEXT,
    head_sha      TEXT,
    merge_sha     TEXT,
    created_at    TEXT NOT NULL,
    merged_at     TEXT,
    closed_at     TEXT,
    updated_at    TEXT,
    url           TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    number        INTEGER PRIMARY KEY,
    title         TEXT,
    state         TEXT NOT NULL,              -- open, closed
    state_reason  TEXT,
    labels        TEXT NOT NULL DEFAULT '[]', -- JSON array of names
    author        TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT,
    updated_at    TEXT,
    comments      INTEGER,
    managed_key        TEXT,                       -- "flaky:<test id>" / "perf:<test id>" on issues greenlight manages
    url           TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_key ON issues(managed_key);

-- Where each sync left off.
CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    synced_at  TEXT NOT NULL
);

-- What has been sent to an OpenTelemetry endpoint, so each export only sends what's new.
CREATE TABLE IF NOT EXISTS otel_exports (
    kind         TEXT NOT NULL,               -- pipeline, run, deployment
    key          TEXT NOT NULL,
    exported_at  TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Claude Code token usage, per session and minute (usage.py). Sessions are Claude Code's ids; a cloud
-- session's claude.ai id (session_...) is what runs.session holds, so remote_session joins the two.
CREATE TABLE IF NOT EXISTS agent_usage (
    session_id          TEXT NOT NULL,
    minute              TEXT NOT NULL,             -- UTC, to the minute
    model               TEXT NOT NULL,
    branch              TEXT NOT NULL DEFAULT '',  -- the git branch the session was on
    requests            INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,  -- the part written for an hour, which costs more
    PRIMARY KEY (session_id, minute, model, branch)
);
CREATE INDEX IF NOT EXISTS idx_agent_usage_minute ON agent_usage(minute);

-- What tool results put into a session's context, and what keeping them there cost (context.py)
CREATE TABLE IF NOT EXISTS agent_context (
    session_id      TEXT NOT NULL,
    minute          TEXT NOT NULL,             -- when the results arrived
    branch          TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL,             -- test, build, git, read, search, web, shell, image, edit, subagent, skill, mcp:<server>, other
    calls           INTEGER NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0,  -- what the results added to the context
    carried_tokens  INTEGER NOT NULL DEFAULT 0,  -- cache reads of them by later requests, until a compaction
    repeat_reads    INTEGER NOT NULL DEFAULT 0,  -- reads of a file already in context and unchanged
    repeat_tokens   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, minute, branch, category)
);
CREATE INDEX IF NOT EXISTS idx_agent_context_minute ON agent_context(minute);

-- Requests that wrote most of the context to the cache again
CREATE TABLE IF NOT EXISTS agent_cache_rebuilds (
    session_id      TEXT NOT NULL,
    agent           TEXT NOT NULL DEFAULT '',  -- '' for the session, else the subagent's transcript
    at              TEXT NOT NULL,
    branch          TEXT NOT NULL DEFAULT '',
    tokens          INTEGER NOT NULL,          -- cache write tokens
    ttl             TEXT,                      -- 1h or 5m: how long the cache lasts
    idle_seconds    INTEGER,                   -- since the request before it
    cause           TEXT NOT NULL,             -- idle, compaction, model, other
    PRIMARY KEY (session_id, agent, at)
);

-- Requests that started work on a new branch while the earlier work was still in context
CREATE TABLE IF NOT EXISTS agent_task_switches (
    session_id      TEXT NOT NULL,
    agent           TEXT NOT NULL DEFAULT '',
    at              TEXT NOT NULL,
    from_branch     TEXT NOT NULL DEFAULT '',
    to_branch       TEXT NOT NULL DEFAULT '',
    context_tokens  INTEGER NOT NULL,          -- the context the new work started with
    carried_tokens  INTEGER NOT NULL,          -- cache reads of it by later requests, until a compaction
    PRIMARY KEY (session_id, agent, at)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id      TEXT PRIMARY KEY,
    remote_session  TEXT,
    repo            TEXT,
    agent           TEXT NOT NULL DEFAULT 'claude-code',
    first_at        TEXT,
    last_at         TEXT,
    updated_at      TEXT NOT NULL
);
