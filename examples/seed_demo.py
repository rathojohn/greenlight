"""Seed a demo DB with 60 days of synthetic history so every view has something to show: test runs
(with reruns until green, the old habit), GitHub Actions pipelines, daily releases, pull requests,
issues (including the ones greenlight would manage) and a perf check with numbers.

    python examples/seed_demo.py --db demo.db
    greenlight --db demo.db flaky
    greenlight --db demo.db gate
    greenlight --db demo.db ui
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr

from greenlight.db import connect, iso, utcnow
from greenlight.ingest import TestResult, ingest_files, record_run

STABLE = [f"tests.test_api::test_endpoint_{i}" for i in range(30)]
FLAKY = {  # test_id -> failure probability
    "tests.test_e2e::test_login_redirect": 0.15,
    "tests.test_worker::test_queue_drains": 0.08,
    "tests.test_e2e::test_search_autocomplete": 0.25,
}
SLOWING = "tests.test_reports::test_monthly_rollup"
BREAKS = "tests.test_billing::test_invoice_total"
PERF = "perf::frame time under 25ms at 4x throttle"


def junit(cases: list[tuple[str, str, float, str | None]]) -> str:
    rows = []
    for test_id, outcome, secs, msg in cases:
        cls, name = test_id.split("::")
        inner = f"<failure message={quoteattr(msg or 'failed')}/>" if outcome == "fail" else ""
        rows.append(f'<testcase classname="{cls}" name="{name}" time="{secs:.3f}">{inner}</testcase>')
    return f'<?xml version="1.0"?><testsuite name="demo">{"".join(rows)}</testsuite>'


def simulate_run(rng: random.Random, day_index: int, total_days: int, last_sha: bool):
    cases = []
    for t in STABLE:
        cases.append((t, "pass", rng.gauss(0.12, 0.01), None))
    for t, p in FLAKY.items():
        if rng.random() < p:
            msg = rng.choice(["TimeoutError: waited 5000ms for selector", "ConnectionResetError: peer reset"])
            cases.append((t, "fail", rng.gauss(5.0, 0.2), msg))
            if rng.random() < 0.5:  # in-run retry that passes, pytest-rerunfailures style
                cases.append((t, "pass", rng.gauss(0.9, 0.05), None))
        else:
            cases.append((t, "pass", rng.gauss(0.9, 0.05), None))
    slow = 2.0 * (2.2 if day_index >= total_days - 3 else 1.0)
    cases.append((SLOWING, "pass", rng.gauss(slow, 0.08), None))
    if last_sha:
        cases.append((BREAKS, "fail", 0.05, "AssertionError: expected 1180.00 got 1080.00"))
    else:
        cases.append((BREAKS, "pass", rng.gauss(0.05, 0.005), None))
    return cases


def add_pipeline(conn, rng, run_no, attempt, sha, when, failed_e2e, sha_prefix="ci"):
    pid = f"gha:{run_no}:{attempt}"
    jobs = [("lint", 40, False), ("test (3.11)", 190, False), ("test (3.12)", 185, False), ("e2e", 420, failed_e2e)]
    queue = int(rng.uniform(4, 40) * 1000)
    start = when + timedelta(milliseconds=queue)
    end = start
    rows = []
    for k, (name, secs, bad) in enumerate(jobs):
        js = start + timedelta(seconds=0 if k < 3 else 45)
        dur = int(rng.gauss(secs, secs * 0.08) * 1000)
        je = js + timedelta(milliseconds=dur)
        end = max(end, je)
        rows.append((f"gha:{run_no}{attempt}{k}", pid, name, "failure" if bad else "success", iso(js), iso(je), dur,
                     int(rng.uniform(1, 9) * 1000), "GitHub Actions 2", None))
    status = "failure" if failed_e2e else "success"
    conn.execute("INSERT OR REPLACE INTO pipelines VALUES (?, 'github-actions', 'CI', ?, ?, 'push', 'main', ?, ?, ?, ?, ?, ?, ?, 'demo', NULL)",
                 (pid, run_no, attempt, sha, status, iso(when), iso(start), iso(end),
                  int((end - start).total_seconds() * 1000), queue))
    conn.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    for job in rows:
        steps = [("Set up job", 2), ("Checkout", 3), ("Install", 25), ("Run tests", max(1, job[6] // 1000 - 32)), ("Upload results", 2)]
        t0 = datetime.fromisoformat(job[4])
        offsets = [sum(x for _, x in steps[:i]) for i in range(len(steps))]
        conn.executemany("INSERT OR REPLACE INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                         [(job[0], i + 1, n, job[3] if n == "Run tests" else "success", s * 1000,
                           iso(t0 + timedelta(seconds=offsets[i]))) for i, (n, s) in enumerate(steps)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    start = utcnow() - timedelta(days=a.days)  # so the whole last simulated day is in the past
    runs = 0
    commits: list[tuple[str, object]] = []
    with closing(connect(a.db)) as conn, tempfile.TemporaryDirectory() as tmp:
        for d in range(a.days):
            for k in range(3):  # three commits a day
                sha = f"{rng.getrandbits(160):040x}"
                last = d == a.days - 1 and k == 2
                committed = start + timedelta(days=d, hours=8 + 3 * k, minutes=rng.randint(0, 50))
                commits.append((sha, committed))
                for attempt in range(1, 4):  # old habit: rerun until green, max 3
                    cases = simulate_run(rng, d, a.days, last)
                    path = Path(tmp) / f"run-{runs}.xml"
                    path.write_text(junit(cases))
                    when = start + timedelta(days=d, hours=9 + 3 * k, minutes=10 * attempt)
                    ingest_files(conn, [path], commit_sha=sha, branch="main", attempt=attempt,
                                 source="demo", external_id=f"demo-{runs}", started_at=when)
                    runs += 1
                    final = {}
                    for t, o, _, _ in cases:
                        final[t] = o
                    e2e_failed = any(o != "pass" for t, o in final.items() if t.startswith("tests.test_e2e"))
                    with conn:
                        add_pipeline(conn, rng, 1000 + d * 3 + k, attempt, sha, when, e2e_failed)
                    if all(o == "pass" for o in final.values()):
                        break
            # the perf check: a number per day against its base build, slower for the last three days
            base = rng.gauss(21.0, 0.6)
            value = base * (1.32 if d >= a.days - 3 else rng.uniform(0.95, 1.06))
            slower = d >= a.days - 3
            record_run(conn, [TestResult(PERF, None, "pass", None, flags="slower" if slower else None,
                                         message=f"median {value:.1f} ms; base {base:.1f} ms" if slower else None)],
                       commit_sha=commits[-1][0], branch="main", source="playtest-report",
                       external_id=f"demo-perf-{d}", started_at=start + timedelta(days=d, hours=7),
                       metrics=[(PERF, "value_ms", round(value, 2)), (PERF, "base_ms", round(base, 2)),
                                (PERF, "budget_ms", 25.0)])

        # a release each weekday, after that day's commits, ships them
        with conn:
            prev = None
            for d in range(a.days):
                day = start + timedelta(days=d)
                if day.weekday() >= 5:
                    continue
                at = day + timedelta(hours=17)
                shipped = [(s, c) for s, c in commits if (prev is None or c > prev) and c <= at]
                prev = at
                if not shipped:
                    continue
                did = f"demo-deploy-{d}"
                conn.execute("INSERT OR REPLACE INTO deployments VALUES (?, 'branch-push', 'release', ?, NULL, ?, 'success', ?, NULL)",
                             (did, shipped[-1][0], iso(at), f"0.{d // 7}.{d % 7}"))
                conn.executemany("INSERT OR REPLACE INTO deploy_commits VALUES (?, ?, ?)", [(did, s, iso(c)) for s, c in shipped])
            # pull requests: one per commit, opened a few hours before it merged
            for n, (s, c) in enumerate(commits, start=1):
                opened = c - timedelta(hours=rng.uniform(0.5, 9))
                state = "open" if n > len(commits) - 2 else "merged"
                conn.execute("INSERT OR REPLACE INTO pull_requests (number, title, author, state, base, head, head_sha, created_at, merged_at, updated_at) "
                             "VALUES (?, ?, 'demo', ?, 'main', ?, ?, ?, ?, ?)",
                             (n, f"Change {n}", state, f"work-{n}", s, iso(opened), iso(c) if state == "merged" else None, iso(c)))
            # issues: two bugs after releases (incidents), the ones greenlight manages, and some plain ones
            now = utcnow()
            issues = [
                (901, "Login loops back to the title after an update", ["bug"], now - timedelta(days=9, hours=5), now - timedelta(days=9), None),
                (902, "Invoices round the wrong way", ["bug"], now - timedelta(days=3, hours=20), now - timedelta(days=3, hours=14), None),
                (903, "Flaky test: tests.test_e2e: test_search_autocomplete", ["flaky-test", "quarantined"], now - timedelta(days=20), None,
                 "flaky:tests.test_e2e::test_search_autocomplete"),
                (904, "Flaky test: tests.test_e2e: test_login_redirect", ["flaky-test"], now - timedelta(days=12), None,
                 "flaky:tests.test_e2e::test_login_redirect"),
                (905, f"Perf regression: {PERF.replace('::', ': ', 1)}", ["perf-regression"], now - timedelta(days=2), None, f"perf:{PERF}"),
                (906, "Settings menu clips on small phones", ["ui"], now - timedelta(days=30), None, None),
                (907, "Add a colorblind palette option", ["enhancement"], now - timedelta(days=16), now - timedelta(days=4), None),
            ]
            for n, title, labels, created, closed, key in issues:
                conn.execute("INSERT OR REPLACE INTO issues (number, title, state, labels, created_at, closed_at, updated_at, managed_key) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (n, title, "closed" if closed else "open", json.dumps(labels), iso(created),
                              iso(closed) if closed else None, iso(closed or created), key))
            conn.execute("INSERT OR REPLACE INTO quarantine VALUES (?, 'labeled quarantined on issue #903', ?, 'github#903')",
                         ("tests.test_e2e::test_search_autocomplete", iso(now - timedelta(days=19))))
            for source in ("pulls", "issues", "actions", "deployments"):
                conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?, NULL, ?)", (source, iso(now - timedelta(minutes=12))))
    print(f"seeded {runs} runs, {len(commits)} commits and their pipelines, releases, PRs and issues into {a.db}")


if __name__ == "__main__":
    main()
