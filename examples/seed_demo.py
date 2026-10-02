"""Seed a demo DB with 60 days of synthetic JUnit runs so every tool has something to show.

    python examples/seed_demo.py --db demo.db
    flakewatch --db demo.db flaky
    flakewatch --db demo.db gate
"""
from __future__ import annotations

import argparse
import random
import tempfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr

from flakewatch.db import connect, utcnow
from flakewatch.ingest import ingest_files

STABLE = [f"tests.test_api::test_endpoint_{i}" for i in range(30)]
FLAKY = {  # test_id -> failure probability
    "tests.test_e2e::test_login_redirect": 0.15,
    "tests.test_worker::test_queue_drains": 0.08,
    "tests.test_e2e::test_search_autocomplete": 0.25,
}
SLOWING = "tests.test_reports::test_monthly_rollup"
BREAKS = "tests.test_billing::test_invoice_total"


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    start = (utcnow() - timedelta(days=a.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    runs = 0
    with closing(connect(a.db)) as conn, tempfile.TemporaryDirectory() as tmp:
        for d in range(a.days):
            for k in range(3):  # three commits a day
                sha = f"{rng.getrandbits(40):010x}"
                last = d == a.days - 1 and k == 2
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
                    if all(o == "pass" for o in final.values()):
                        break
    print(f"seeded {runs} runs into {a.db}")


if __name__ == "__main__":
    main()
