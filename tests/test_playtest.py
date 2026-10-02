import json
import subprocess
from pathlib import Path

import pytest

from greenlight import analysis, playtest
from greenlight.gitrepo import Repo

SHA = "8d285fc583d476c06eac311f39842ed2de85328b"


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def record(when, commit, suites, changed=None, session="session_01abc", command="npm run test:changed"):
    return {"when": when, "commit": commit, "changed": changed or {}, "session": session, "command": command,
            "suites": suites}


def write_record(repo: Path, rec: dict) -> str:
    name = playtest.record_name(rec["when"], rec["commit"])
    d = repo / playtest.RUNS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(rec, indent=2))
    return name


@pytest.fixture
def game(tmp_path):
    """A repo with main, a work branch, records on both and one only in the working tree."""
    repo = tmp_path / "game"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.email", "t@t")
    sh(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("game\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", "init")
    head = sh(repo, "rev-parse", "HEAD")
    # main: smoke fails one check, then a --rerun on the same commit passes it: a flip
    write_record(repo, record("2026-10-01T21:31:39.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": ["the Lantern points at the foe it locked on"]},
        "studio": {"pass": True, "checks": 14}}))
    write_record(repo, record("2026-10-01T21:43:41.000Z", head, {"smoke": {"pass": True, "checks": 38}},
                              command="node tools/playtest/run.cjs smoke --rerun"))
    known = repo / playtest.KNOWN
    known.write_text(json.dumps({"menu: the tall phone fits": "M10-01: waits on the phone layout"}))
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", "records")
    sh(repo, "checkout", "-qb", "claude/work-1")
    # the branch: smoke crashes, perf runs slower than its base
    write_record(repo, record("2026-10-02T01:14:34.844Z", head, {
        "smoke": {"pass": False, "checks": 12, "failed": [playtest.CRASH_CHECK]},
        "perf": {"pass": True, "checks": 7, "slower": ["logic under 2ms at 600 enemies"], "compare": head}},
        changed={"src/x.ts": "abc123"}))
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", "branch record")
    sh(repo, "checkout", "-q", "main")
    return repo, head


def test_reads_records_from_every_branch_once(game):
    repo, head = game
    recs = playtest.read_records(Repo(str(repo)))
    assert len(recs) == 3
    assert [r.data["when"][:16] for r in recs] == ["2026-10-01T21:31", "2026-10-01T21:43", "2026-10-02T01:14"]
    assert recs[2].branch == "claude/work-1" and recs[0].branch == "main"
    assert recs[0].code_id == head
    assert recs[2].code_id.startswith(head + "+") and len(recs[2].code_id) == len(head) + 9


def test_sync_detects_the_flip_and_infers_passes(db, game):
    conn, _ = db
    repo, head = game
    out = playtest.sync(conn, str(repo))
    assert out["records"] == 3 and out["new_runs"] == 3 and out["known_failures"] == 1
    lantern = "smoke::the Lantern points at the foe it locked on"
    s = analysis.flake_stats(conn, 3650)[lantern]
    assert (s["flip_shas"], s["eligible_shas"]) == (1, 1)
    flags = dict(conn.execute("""SELECT ru.command, r.flags FROM results r JOIN runs ru USING (run_id)
                                 WHERE r.test_id = ?""", (lantern,)).fetchall())
    assert flags == {"npm run test:changed": None, "node tools/playtest/run.cjs smoke --rerun": "inferred"}
    # the crashed smoke run infers nothing, so the lantern check has no row there
    crashed = conn.execute("SELECT COUNT(*) FROM results WHERE test_id = ? AND run_id = "
                           "(SELECT run_id FROM runs WHERE branch = 'claude/work-1')", (lantern,)).fetchone()[0]
    assert crashed == 0
    slower = conn.execute("SELECT outcome, flags FROM results WHERE test_id LIKE 'perf::%'").fetchone()
    assert tuple(slower) == ("pass", "slower")
    totals = [r[0] for r in conn.execute("SELECT total_tests FROM runs ORDER BY started_at")]
    assert totals == [52, 38, 19]
    q = conn.execute("SELECT added_by FROM quarantine WHERE test_id = 'menu::the tall phone fits'").fetchone()
    assert q["added_by"] == playtest.KNOWN_BY


def test_sync_is_idempotent_and_backfills_new_watched_checks(db, game):
    conn, _ = db
    repo, head = game
    playtest.sync(conn, str(repo))
    again = playtest.sync(conn, str(repo))
    assert again["new_runs"] == 0 and again["inferred_results_added"] == 0
    # a new record fails a check nobody had seen fail: older runs where studio ran get an inferred pass
    write_record(repo, record("2026-10-02T03:00:00.000Z", head, {
        "studio": {"pass": False, "checks": 14, "failed": ["the frame on screen matches the clock"]}},
        changed={"src/clock.ts": "f00d"}))
    third = playtest.sync(conn, str(repo))
    assert third["new_runs"] == 1 and third["inferred_results_added"] == 1
    t = analysis.triage_run(conn, run_id=playtest.latest_local_run(conn, str(repo)))
    # new code, no flake history: a real failure (records name only failures, so never "new_test")
    assert t["decision"] == "REAL_FAILURE"
    assert t["failures"][0]["category"] == "real_failure"


def test_gate_reruns_a_known_flake(db, game):
    conn, _ = db
    repo, head = game
    lantern = "the Lantern points at the foe it locked on"
    write_record(repo, record("2026-10-02T04:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [lantern]}}, changed={"a": "1"}))
    write_record(repo, record("2026-10-02T04:10:00.000Z", head, {"smoke": {"pass": True, "checks": 38}},
                              changed={"a": "1"}))
    write_record(repo, record("2026-10-02T05:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [lantern]}}, changed={"b": "2"}))
    playtest.sync(conn, str(repo))
    t = analysis.triage_run(conn, run_id=playtest.latest_local_run(conn, str(repo)))
    assert t["decision"] == "RERUN_TARGETED", t
    assert playtest.rerun_command(t["rerun_tests"]) == "node tools/playtest/run.cjs smoke --rerun"


def test_report_upgrades_the_matching_record(db, game):
    conn, _ = db
    repo, head = game
    when = "2026-10-02T06:00:00.000Z"
    write_record(repo, record(when, head, {"perf": {"pass": True, "checks": 2, "slower": ["early: worst frame"]}}))
    out = repo / "tools/playtest/out"
    out.mkdir(parents=True)
    (out / "report.json").write_text(json.dumps({"when": when, "commit": head[:7], "suites": [{
        "suite": "perf", "secs": 61, "runs": 3, "crashed": False, "checks": [
            {"name": "early: worst frame", "ok": False, "slower": True, "value": 31.5, "base": 22.1, "budget": 25,
             "detail": "median 31.5 ms of 30, 31.5, 33; origin/main 22.1 ms"},
            {"name": "logic under 2ms", "ok": True, "value": 1.4, "base": 1.5, "budget": 2, "detail": "median 1.4"},
        ]}]}))
    res = playtest.sync(conn, str(repo))
    assert res["report"]["upgraded"] is True and res["report"]["checks"] == 2
    run_id = playtest.latest_local_run(conn, str(repo))
    run = conn.execute("SELECT source, total_tests, duration_ms FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert tuple(run) == ("playtest-report", None, 61000)
    rows = {r["test_id"]: (r["outcome"], r["flags"]) for r in conn.execute(
        "SELECT * FROM results WHERE run_id = ?", (run_id,))}
    assert rows == {"perf::early: worst frame": ("pass", "slower"), "perf::logic under 2ms": ("pass", None)}
    m = dict(((t, n), v) for t, n, v in conn.execute(
        "SELECT test_id, name, value FROM metrics WHERE run_id = ?", (run_id,)))
    assert m[("perf::early: worst frame", "value_ms")] == 31.5 and m[("perf::early: worst frame", "base_ms")] == 22.1
    assert m[("perf", "duration_ms")] == 61000
    # a second sync leaves the upgraded run alone
    assert playtest.sync(conn, str(repo))["report"]["upgraded"] is False
    assert conn.execute("SELECT COUNT(*) FROM results WHERE run_id = ?", (run_id,)).fetchone()[0] == 2


def test_known_failures_mirror_removes_only_its_own_rows(db, game):
    conn, _ = db
    repo, _ = game
    playtest.sync(conn, str(repo))
    analysis.quarantine(conn, "smoke::the Lantern points at the foe it locked on", "flaky, issue 50")
    (repo / playtest.KNOWN).write_text("{}")
    sh(repo, "commit", "-qam", "clear known failures")
    playtest.sync(conn, str(repo))
    rows = {r["test_id"]: r["added_by"] for r in conn.execute("SELECT * FROM quarantine")}
    assert rows == {"smoke::the Lantern points at the foe it locked on": "manual"}


def test_sync_rejects_a_non_repo(db, tmp_path):
    conn, _ = db
    with pytest.raises(ValueError):
        playtest.sync(conn, str(tmp_path))
