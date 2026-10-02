import pytest

from flakewatch import analysis

STABLE = [("t.api::a", "pass", 0.1, None), ("t.api::b", "pass", 0.1, None)]
FLAKY = "t.e2e::login"
BREAKS = "t.billing::total"


def flaky_history(rec, commits: int = 3):
    """FLAKY fails then passes on a rerun of the same commit, on `commits` commits."""
    for i in range(commits):
        sha = f"sha{i}"
        rec.run(sha, STABLE + [(FLAKY, "fail", 5.0, "TimeoutError: waited 5000ms")])
        rec.run(sha, STABLE + [(FLAKY, "pass", 0.9, None)])


def test_wilson_ranks_more_evidence_higher():
    assert analysis.wilson_lower(5, 10) > analysis.wilson_lower(1, 1)
    assert analysis.wilson_lower(0, 0) == 0.0


def test_flake_stats_counts_flips_only_on_rerun_commits(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    rec.run("solo", STABLE + [(FLAKY, "fail", 5.0, "x")])  # one run only: not eligible
    s = analysis.flake_stats(conn)[FLAKY]
    assert (s["flip_shas"], s["eligible_shas"], s["classification"]) == (3, 3, "flaky")
    assert analysis.flake_stats(conn)["t.api::a"]["classification"] == "stable"


def test_gate_pass_when_everything_passes(db, rec):
    conn, _ = db
    rec.run("s1", STABLE)
    t = analysis.triage_run(conn)
    assert (t["decision"], t["exit_code"], t["failures"]) == ("PASS", 0, [])


def test_gate_reruns_known_flaky_only(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    rec.run("new", STABLE + [(FLAKY, "fail", 5.0, "TimeoutError: waited 3000ms")])
    t = analysis.triage_run(conn)
    assert t["decision"] == "RERUN_TARGETED" and t["exit_code"] == 2
    assert t["rerun_tests"] == [FLAKY]
    assert t["failures"][0]["category"] == "known_flaky"
    assert t["failures"][0]["new_signature"] is False


def test_gate_blocks_on_stable_test_failure(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    rec.run("s9", STABLE + [(BREAKS, "pass", 0.1, None)])
    rec.run("s10", STABLE + [(BREAKS, "fail", 0.1, "AssertionError: 1180 != 1080"),
                            (FLAKY, "fail", 5.0, "TimeoutError: waited 5000ms")])
    t = analysis.triage_run(conn)
    assert t["decision"] == "REAL_FAILURE" and t["exit_code"] == 1
    assert t["blocking"] == [BREAKS] and t["rerun_tests"] == [FLAKY]


def test_new_test_blocks(db, rec):
    conn, _ = db
    rec.run("s1", STABLE)
    rec.run("s2", STABLE + [("t.new::x", "fail", 0.1, "nope")])
    assert analysis.triage_run(conn)["failures"][0]["category"] == "new_test"


def test_flaky_test_that_never_passes_on_a_commit_is_promoted(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    for _ in range(3):
        rec.run("dead", STABLE + [(FLAKY, "fail", 5.0, "TimeoutError: waited 5000ms")])
    t = analysis.triage_run(conn)
    assert t["failures"][0]["category"] == "real_failure" and t["decision"] == "REAL_FAILURE"


def test_quarantined_failure_is_ignored(db, rec):
    conn, _ = db
    rec.run("s1", STABLE + [(BREAKS, "pass", 0.1, None)])
    rec.run("s2", STABLE + [(BREAKS, "fail", 0.1, "x")])
    analysis.quarantine(conn, BREAKS, "known broken, ticket 12")
    t = analysis.triage_run(conn)
    assert t["decision"] == "PASS" and t["failures"][0]["category"] == "quarantined"
    assert analysis.unquarantine(conn, BREAKS) is True
    assert analysis.triage_run(conn)["decision"] == "REAL_FAILURE"


def test_retry_inside_a_run_counts_as_flip_and_does_not_block(db, rec):
    conn, _ = db
    rec.run("s1", STABLE + [(FLAKY, "fail", 5.0, "x"), (FLAKY, "pass", 0.9, None)])
    t = analysis.triage_run(conn)
    assert t["decision"] == "PASS" and t["passed_on_retry"] == [FLAKY]
    assert analysis.flake_stats(conn)[FLAKY]["flip_shas"] == 1


def test_sweep_suggests_and_applies(db, rec):
    conn, _ = db
    flaky_history(rec, 3)
    dry = analysis.sweep(conn)
    assert [s["test_id"] for s in dry["to_quarantine"]] == [FLAKY] and not dry["applied"]
    assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
    analysis.sweep(conn, apply=True)
    row = conn.execute("SELECT added_by FROM quarantine WHERE test_id = ?", (FLAKY,)).fetchone()
    assert row["added_by"] == "auto"


def test_resolve_test_id_by_substring_and_errors(db, rec):
    conn, _ = db
    flaky_history(rec, 1)
    assert analysis.resolve_test_id(conn, "login") == FLAKY
    with pytest.raises(LookupError):
        analysis.resolve_test_id(conn, "t.api")  # ambiguous
    with pytest.raises(LookupError):
        analysis.resolve_test_id(conn, "nope")


def test_run_query_is_read_only(db, rec):
    conn, _ = db
    rec.run("s1", STABLE)
    out = analysis.run_query(conn, "SELECT COUNT(*) AS n FROM results")
    assert out["rows"] == [[2]]
    for bad in ("DELETE FROM runs", "SELECT 1; DELETE FROM runs", "PRAGMA table_info(runs)"):
        with pytest.raises(ValueError):
            analysis.run_query(conn, bad)
