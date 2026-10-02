import json

import pytest

from greenlight import issues
from greenlight.github import GitHub
from tests.fakegithub import FakeGitHub
from tests.test_analysis import FLAKY, STABLE, flaky_history

OPTS = {"window_days": 30, "min_flips": 2}


@pytest.fixture
def fake():
    f = FakeGitHub("o/r")
    yield f
    f.close()


def add_issue(conn, n, title, state="open", key=None, closed_at=None, labels=()):
    conn.execute("INSERT INTO issues (number, title, state, labels, created_at, closed_at, managed_key) VALUES (?,?,?,?,?,?,?)",
                 (n, title, state, json.dumps(list(labels)), "2026-01-01T00:00:00+00:00", closed_at, key))


def slower_run(rec, sha, value=31.5, base=22.1):
    from greenlight.ingest import TestResult, record_run
    record_run(rec.conn, [TestResult("perf::early: worst frame under 25ms", None, "pass", None,
                                     message="median 31.5 ms", flags="slower")],
               commit_sha=sha, source="playtest-report",
               metrics=[("perf::early: worst frame under 25ms", "value_ms", value),
                        ("perf::early: worst frame under 25ms", "base_ms", base)])


def test_plan_creates_one_issue_per_flaky_test_and_perf_regression(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    slower_run(rec, "p1")
    acts = issues.plan(conn, OPTS)
    kinds = sorted((a.kind, a.key) for a in acts)
    assert kinds == [("create", f"flaky:{FLAKY}"), ("create", "perf:perf::early: worst frame under 25ms")]
    flaky = next(a for a in acts if a.key.startswith("flaky:"))
    assert flaky.title == "Flaky test: t.e2e: login" and flaky.labels == ["flaky-test"]
    assert issues.marker(f"flaky:{FLAKY}") in flaky.body and "3 of 3" in flaky.body
    perf = next(a for a in acts if a.key.startswith("perf:"))
    assert "31.5 ms" in perf.body and "22.1 ms" in perf.body and perf.labels == ["perf-regression"]


def test_plan_updates_open_reopens_after_new_flips_and_skips_old_closed(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    add_issue(conn, 7, "Flaky test: t.e2e: login", key=f"flaky:{FLAKY}")
    assert [(a.kind, a.number) for a in issues.plan(conn, OPTS)] == [("update", 7)]
    conn.execute("UPDATE issues SET state = 'closed', closed_at = '2000-01-01T00:00:00+00:00' WHERE number = 7")
    assert [(a.kind, a.number) for a in issues.plan(conn, OPTS)] == [("reopen", 7)]
    conn.execute("UPDATE issues SET closed_at = '2999-01-01T00:00:00+00:00' WHERE number = 7")
    assert issues.plan(conn, OPTS) == []


def test_plan_links_a_hand_made_issue_instead_of_duplicating(db, rec):
    conn, _ = db
    slower_run(rec, "p1")
    add_issue(conn, 44, "Perf: early worst frame jumped under 25ms throttle")
    acts = issues.plan(conn, OPTS)
    assert [(a.kind, a.number) for a in acts] == [("link", 44)]


def test_healed_comment_when_clean_long_enough(db, rec):
    conn, _ = db
    add_issue(conn, 9, "Flaky test: t.e2e: login", key=f"flaky:{FLAKY}")
    for i in range(10):
        rec.run(f"clean{i}", STABLE + [(FLAKY, "pass", 0.9, None)])
    acts = issues.plan(conn, OPTS)
    assert [(a.kind, a.number) for a in acts] == [("healed", 9)]
    assert issues.HEALED in acts[0].comment


def test_apply_against_github(db, rec, fake):
    conn, _ = db
    flaky_history(rec, 3)
    slower_run(rec, "p1")
    add_issue(conn, 44, "Perf: early worst frame jumped under 25ms throttle")
    fake.route("GET", "{repo}/labels/flaky-test", lambda q, b: (404, {"message": "Not Found"}))
    fake.route("POST", "{repo}/labels", lambda q, b: (201, b))
    fake.route("POST", "{repo}/issues", lambda q, b: (201, {"number": 51, "title": b["title"], "body": b["body"],
                                                            "state": "open", "labels": b["labels"],
                                                            "created_at": "2026-10-02T00:00:00Z"}))
    fake.route("GET", "{repo}/issues/44", {"number": 44, "title": "Perf: early", "body": "numbers", "state": "open",
                                           "created_at": "2026-10-01T00:00:00Z"})
    fake.route("PATCH", "{repo}/issues/44", lambda q, b: (200, {"number": 44, "title": "Perf: early", "body": b["body"],
                                                                "state": "open", "created_at": "2026-10-01T00:00:00Z"}))
    gh = GitHub("o/r", "t", fake.url)
    done = issues.apply(conn, gh, issues.plan(conn, OPTS))
    assert sorted((d["kind"], d["number"], d["result"]) for d in done) == [("create", 51, "done"), ("link", 44, "done")]
    assert fake.calls("POST", "{repo}/labels")[0]["body"]["name"] == "flaky-test"
    patched = fake.calls("PATCH", "{repo}/issues/44")[0]["body"]["body"]
    assert patched.startswith("<!-- greenlight:perf:perf::early: worst frame under 25ms -->\nnumbers")
    keys = dict(conn.execute("SELECT number, managed_key FROM issues").fetchall())
    assert keys[51] == f"flaky:{FLAKY}" and keys[44] == "perf:perf::early: worst frame under 25ms"
    # the next plan finds both and only refreshes them
    assert sorted(a.kind for a in issues.plan(conn, OPTS)) == ["update", "update"]


def test_update_is_a_noop_when_only_the_stamp_changed(db, rec, fake):
    conn, _ = db
    flaky_history(rec, 3)
    add_issue(conn, 7, "Flaky test: t.e2e: login", key=f"flaky:{FLAKY}")
    (act,) = issues.plan(conn, OPTS)
    # the same content under an older "last updated" stamp
    old = issues.re.sub(r"greenlight, last [\d: -]+ UTC", "greenlight, last 1999-01-01 00:00 UTC", act.body)
    assert old != act.body
    fake.route("GET", "{repo}/issues/7", {"number": 7, "body": old, "state": "open"})
    done = issues.apply(conn, GitHub("o/r", "t", fake.url), [act])
    assert done[0]["result"] == "unchanged" and not fake.calls("PATCH", "{repo}/issues/7")
