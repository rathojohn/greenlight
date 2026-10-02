from greenlight.ingest import assign_retries, failure_signature, ingest_files, parse_junit


def test_parse_outcomes_and_ids(tmp_path):
    p = tmp_path / "r.xml"
    p.write_text(
        '<testsuites><testsuite name="s">'
        '<testcase classname="pkg.mod" name="ok" time="0.5"/>'
        '<testcase classname="pkg.mod" name="bad" time="1.25"><failure message="boom 42"/></testcase>'
        '<testcase classname="pkg.mod" name="err"><error>Traceback</error></testcase>'
        '<testcase name="bare"><skipped/></testcase>'
        "</testsuite></testsuites>")
    rows = parse_junit(p)
    assert [(r.test_id, r.outcome, r.duration_ms) for r in rows] == [
        ("pkg.mod::ok", "pass", 500),
        ("pkg.mod::bad", "fail", 1250),
        ("pkg.mod::err", "error", None),
        ("bare", "skip", None),
    ]
    assert rows[1].message == "boom 42" and rows[1].failure_sig
    assert rows[0].failure_sig is None


def test_surefire_flaky_children_become_retries(tmp_path):
    p = tmp_path / "r.xml"
    p.write_text(
        '<testsuite><testcase classname="A" name="t" time="1">'
        '<flakyFailure message="timeout 1" time="2"/><flakyError message="reset" time="3"/>'
        "</testcase></testsuite>")
    rows = assign_retries(parse_junit(p))
    assert [(r.outcome, r.retry) for r in rows] == [("fail", 0), ("error", 1), ("pass", 2)]


def test_duplicate_testcases_are_retries_in_order(tmp_path):
    p = tmp_path / "r.xml"
    p.write_text(
        '<testsuite><testcase classname="A" name="t"><failure message="x"/></testcase>'
        '<testcase classname="A" name="t"/></testsuite>')
    rows = assign_retries(parse_junit(p))
    assert [(r.outcome, r.retry) for r in rows] == [("fail", 0), ("pass", 1)]


def test_signature_ignores_numbers_and_quoted_values():
    a = failure_signature("TimeoutError: waited 5000ms for 'button#login'")
    b = failure_signature("TimeoutError: waited 3000ms for 'div.search'")
    c = failure_signature("AssertionError: expected 1 got 2")
    assert a == b != c
    assert failure_signature("") is None and failure_signature(None) is None


def test_ingest_is_idempotent_by_external_id_and_counts_attempts(db, tmp_path):
    conn, _ = db
    p = tmp_path / "r.xml"
    p.write_text('<testsuite><testcase classname="A" name="t"/></testsuite>')
    r1, created1, n = ingest_files(conn, [p], commit_sha="abc", external_id="ci-1")
    r2, created2, _ = ingest_files(conn, [p], commit_sha="abc", external_id="ci-1")
    r3, _, _ = ingest_files(conn, [p], commit_sha="abc")
    assert (created1, created2, n) == (True, False, 1)
    assert r1 == r2 != r3
    attempts = [r[0] for r in conn.execute("SELECT attempt FROM runs ORDER BY run_id")]
    assert attempts == [1, 2]


def test_empty_report_is_an_error(db, tmp_path):
    conn, _ = db
    p = tmp_path / "r.xml"
    p.write_text("<testsuite/>")
    try:
        ingest_files(conn, [p], commit_sha="abc")
    except ValueError as e:
        assert "No <testcase>" in str(e)
    else:
        raise AssertionError("expected ValueError")
