# flakewatch

Local flaky-test tracking for a personal project. Replaces "rerun the whole regression every time something goes red" with a gate that knows which failures are flaky. Built in a claude.ai chat, moved here to keep going.

## Layout

- `flakewatch/schema.sql`: SQLite schema (runs, results, quarantine). WAL mode.
- `flakewatch/db.py`: connections. DB path is `--db`, then `$FLAKEWATCH_DB`, then `~/.flakewatch/flakewatch.db`.
- `flakewatch/ingest.py`: JUnit XML parser. Handles duplicate testcases as retries and Surefire `flakyFailure`/`rerunFailure`.
- `flakewatch/analysis.py`: flake stats, `triage_run` (the gate), quarantine, sweep, read-only SQL.
- `flakewatch/forecast.py`: Toto 2.0 forecasting (duration regressions, suite metrics). Optional dependency.
- `flakewatch/dashboard.py` + `web.py` + `ui/index.html`: local dashboard and `--export` snapshot.
- `flakewatch/server.py`: MCP server (stdio), 9 tools prefixed `flakewatch_`.
- `flakewatch/cli.py`: `ingest`, `gate`, `flaky`, `sweep`, `trends`, `ui`.
- `examples/seed_demo.py`: 60 days of synthetic runs.

## Commands

```
pip install -e ".[toto]"          # Toto needs Python 3.12+
python examples/seed_demo.py --db demo.db
flakewatch --db demo.db gate      # exit 0 PASS, 2 RERUN_TARGETED, 1 REAL_FAILURE, 3 error
flakewatch --db demo.db ui
```

There is no automated test suite yet. Everything was verified by hand in a sandbox: CLI, MCP over stdio, every UI view at desktop and phone widths, and the quarantine flows.

## Decisions worth knowing

- A flip is a commit where a test both passed and failed. Only commits where the test ran 2+ times count toward the denominator. Flaky = flips on 2+ commits in the window, suspect = 1. Ranking uses the Wilson lower bound.
- Gate: quarantined failures are ignored, known/suspect flaky failures get a targeted rerun, new tests and tests without flake history block. A flaky test that fails 3+ times on one commit without passing is treated as real.
- Toto is only used for continuous signals (durations, rerun volume, failure rate). Flake classification is counting, not forecasting.
- Toto 2.0: `pip install toto-models`, `from toto2 import Toto2Model`, default checkpoint `Datadog/Toto-2.0-22m` (fine on CPU). Context length must be a multiple of the patch size (32), so series are left-padded with masked values.
- MCP SDK 2.x renamed FastMCP to `mcp.server.mcpserver.MCPServer`. `server.py` imports that and falls back to FastMCP for 1.x.
- `ui/index.html` is one file, vanilla JS, hand-rolled SVG charts. Snapshot keys built by `snapKey()` in the page must match `snap_key()` in `web.py`.
- The UI server binds to 127.0.0.1, rejects foreign Host headers, and requires an `X-Flakewatch: 1` header on writes. There is no other auth.

## Known limits

- The DB is local, so hosted CI (GitHub Actions) can't write to it directly.
- The Runs page triages each of the last 50 runs on every load. Fine now, slow with a big history. Caching the decision at ingest would fix it.
- No GitHub or issue tracker integration yet.

## Where we left off

The user asked, and this hasn't been answered yet:

> How do I use this? How does it auth? How does it integrate with GitHub and issues? You said we should DIY so I want a lot of the features you get from Datadog around CI/CD and software development.

Start there. The user is a Principal SE at Datadog and knows that product deeply, so scope Datadog-style CI/CD features with him rather than guessing, and be honest about what's worth building versus buying.

## Writing conventions

- Never use em dashes, in docs, comments, UI copy, or replies.
- No stock AI phrasing ("Here's the thing", "It's not X, it's Y", "And that matters").
- UI copy is sentence case, plain verbs, no all-caps labels.
- Prefer honest trade-offs over hedged or motivational framing.
