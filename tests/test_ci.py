import json

import pytest

from greenlight import ci, cli, github
from greenlight.db import connect
from tests.conftest import hide_clis, junit_xml
from tests.fakegithub import FakeGitHub

FLAKY = "t.e2e::login"


@pytest.fixture
def actions(monkeypatch, tmp_path):
    """Look like a GitHub Actions job on a pull request."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 12}}))
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "abc123", "GITHUB_HEAD_REF": "feature", "GITHUB_REPOSITORY": "o/r",
           "GITHUB_RUN_ID": "900", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW": "CI", "GITHUB_JOB": "test",
           "GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
           "GITHUB_OUTPUT": str(tmp_path / "out.txt"), "GITHUB_SERVER_URL": "https://github.com"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for var in github.TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GREENLIGHT_CONFIG", raising=False)
    hide_clis(monkeypatch)
    return tmp_path


def write_junit(path, cases):
    path.write_text(junit_xml(cases))
    return str(path)


def test_actions_env_reads_the_pull_request(actions):
    env = ci.actions_env()
    assert env["pr"] == 12 and env["sha"] == "abc123" and env["branch"] == "feature"
    assert env["url"] == "https://github.com/o/r/actions/runs/900/attempts/1"


def test_a_job_run_round_trips_as_a_record(actions, tmp_path):
    """What the Action sends to a greenlight server: the job's run as a record, loaded once however often it's sent."""
    with connect(str(tmp_path / "a.db")) as conn:
        res = ci.record_junit(conn, [write_junit(tmp_path / "j.xml", [(FLAKY, "fail", 1.0, "Timeout 5000ms"),
                                                                       ("t.a::ok", "pass", 0.2, None)])],
                              name="py3.12")
        rec = ci.export_run(conn, res["run_id"])
    assert res["external_id"] == "gha:900:1:test:py3.12"
    assert rec["commit_sha"] == "abc123@py3.12" and rec["git_commit"] == "abc123"
    assert len(rec["results"]) == 2 and rec["session"] == "CI / test / py3.12"
    rec = json.loads(json.dumps(rec))  # over the wire
    with connect(str(tmp_path / "b.db")) as fresh:
        assert ci.load_record(fresh, rec) and not ci.load_record(fresh, rec)
        row = fresh.execute("SELECT message, failure_sig FROM results WHERE test_id = ?", (FLAKY,)).fetchone()
        assert row["message"] == "Timeout 5000ms" and row["failure_sig"]


def test_report_markdown_summary_outputs_and_exit_codes(actions, tmp_path, capsys):
    db = str(tmp_path / "r.db")
    for i in range(3):  # FLAKY has a flake history
        for outcome in ("fail", "pass"):
            with connect(db) as conn:
                ci.record_junit(conn, [write_junit(tmp_path / "h.xml", [(FLAKY, outcome, 1.0, "Timeout")])],
                                sha=f"h{i}")
    j = write_junit(tmp_path / "j.xml", [(FLAKY, "fail", 1.0, "Timeout"), ("t.a::ok", "pass", 0.1, None)])
    assert cli.main(["--db", db, "ci", "record", "--junit", j]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "ci", "report"]) == 0  # flaky only: passes with --fail-on real
    summary = (tmp_path / "summary.md").read_text()
    assert "greenlight: Rerun the flaky tests only" in summary and "| `t.e2e::login` | known flaky | 3 of 3 commits |" in summary
    assert "decision=RERUN_TARGETED" in (tmp_path / "out.txt").read_text()
    assert cli.main(["--db", db, "ci", "report", "--fail-on", "any"]) == 2
    # a stable test fails on new code (on the same code it would be a flip)
    j2 = write_junit(tmp_path / "k.xml", [("t.a::ok", "fail", 0.1, "boom")])
    assert cli.main(["--db", db, "ci", "record", "--junit", j2, "--sha", "def456"]) == 0
    assert cli.main(["--db", db, "ci", "report", "--sha", "def456"]) == 1


def test_pr_comment_is_upserted(actions, tmp_path):
    fake = FakeGitHub("o/r")
    try:
        comments = []
        fake.route("GET", "{repo}/issues/12/comments", lambda q, b: (200, comments))
        fake.route("POST", "{repo}/issues/12/comments",
                   lambda q, b: (comments.append({"id": 5, "body": b["body"]}), (201, {"id": 5, "html_url": "u"}))[1])
        fake.route("PATCH", "{repo}/issues/comments/5", lambda q, b: (200, {"id": 5, "html_url": "u"}))
        gh = github.GitHub("o/r", "t", fake.url)
        assert ci.upsert_pr_comment(gh, 12, ci.pr_marker("a") + "\nfirst", "a")["id"] == 5
        ci.upsert_pr_comment(gh, 12, ci.pr_marker("a") + "\nsecond", "a")
        assert len(fake.calls("POST", "{repo}/issues/12/comments")) == 1
        assert fake.calls("PATCH", "{repo}/issues/comments/5")[0]["body"]["body"].endswith("second")
        ci.upsert_pr_comment(gh, 12, ci.pr_marker("b") + "\nother job", "b")  # another matrix job: its own comment
        assert len(fake.calls("POST", "{repo}/issues/12/comments")) == 2
    finally:
        fake.close()


def test_matrix_entries_are_separate_environments(actions, tmp_path):
    from greenlight import analysis
    with connect(str(tmp_path / "m.db")) as conn:
        for name, outcome in (("py3.11", "fail"), ("py3.12", "pass"), ("py3.11", "fail")):
            ci.record_junit(conn, [write_junit(tmp_path / "m.xml", [("t.a::x", outcome, 0.1, "boom")])], name=name)
        s = analysis.flake_stats(conn, 30)["t.a::x"]
        assert s["flip_shas"] == 0  # fails on 3.11 every time, passes on 3.12: not flaky
        ids = [r[0] for r in conn.execute("SELECT external_id FROM runs ORDER BY run_id")]
        assert ids == ["gha:900:1:test:py3.11", "gha:900:1:test:py3.12", "gha:900:1:test:py3.11:2"]
        assert ci.this_job_run(conn, "py3.11") == 3 and ci.this_job_run(conn, "py3.12") == 2
