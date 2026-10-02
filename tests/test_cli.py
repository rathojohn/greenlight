import json

import pytest

from greenlight import cli, github
from tests.fakegithub import FakeGitHub
from tests.test_playtest import game, record, sh, write_record  # noqa: F401 - fixture


@pytest.fixture
def no_token(monkeypatch, tmp_path):
    for var in github.TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GREENLIGHT_CONFIG", raising=False)
    monkeypatch.delenv("GREENLIGHT_DB", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_init_writes_a_config_the_loader_accepts(game, no_token, capsys):  # noqa: F811
    repo, _ = game
    assert cli.main(["init", str(repo)]) == 0
    text = (repo / "greenlight.toml").read_text()
    assert "enabled = true" in text and 'db = "~/.greenlight/game.db"' in text
    assert cli.main(["init", str(repo)]) == 3  # refuses to overwrite
    from greenlight import config
    cfg = config.load(str(repo / "greenlight.toml"))
    assert cfg.playtest_enabled and cfg.git_path == str(repo)


def test_playtest_gate_exit_codes(game, no_token, tmp_path, capsys):  # noqa: F811
    repo, head = game
    db = str(tmp_path / "x.db")
    lantern = "the Lantern points at the foe it locked on"
    write_record(repo, record("2026-10-02T05:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [lantern]}}, changed={"b": "2"}))
    write_record(repo, record("2026-10-02T05:10:00.000Z", head, {"smoke": {"pass": True, "checks": 38}},
                              changed={"b": "2"}))
    write_record(repo, record("2026-10-02T06:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [lantern]}}, changed={"c": "3"}))
    code = cli.main(["--db", db, "playtest", "gate", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "rerun only: node tools/playtest/run.cjs smoke --rerun" in out
    assert cli.main(["--db", db, "playtest", "gate", "--repo", str(repo), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["rerun_command"].endswith("smoke --rerun")


def test_sync_reports_each_source_and_keeps_going(game, no_token, tmp_path, capsys):  # noqa: F811
    repo, _ = game
    fake = FakeGitHub("o/r")
    try:
        fake.pages("GET", "{repo}/pulls", [])
        fake.route("GET", "{repo}/issues", lambda q, b: (500, {"message": "boom"}))
        fake.pages("GET", "{repo}/actions/runs", [], key="workflow_runs")
        cfg = tmp_path / "greenlight.toml"
        cfg.write_text(f'db = "{tmp_path}/s.db"\n[github]\nrepo = "o/r"\napi_url = "{fake.url}"\n'
                       f'[git]\npath = "{repo}"\n[playtest]\nenabled = true\n')
        code = cli.main(["--config", str(cfg), "sync"])
        out = capsys.readouterr().out
        assert code == 1
        assert "playtest     records 3" in out and "pulls        pull_requests 0" in out
        assert "issues       error: GitHub 500" in out
    finally:
        fake.close()


def test_auth_never_prints_the_token(no_token, monkeypatch, tmp_path, capsys):
    fake = FakeGitHub("o/r")
    try:
        fake.route("GET", "/repos/o/r", {"private": True, "permissions": {"admin": False, "push": True, "pull": True}})
        fake.route("GET", "/rate_limit", {"resources": {"core": {"remaining": 4990, "limit": 5000}}})
        monkeypatch.setenv("GH_TOKEN", "ghp_supersecret")
        cfg = tmp_path / "greenlight.toml"
        cfg.write_text(f'[github]\nrepo = "o/r"\napi_url = "{fake.url}"\n')
        assert cli.main(["--config", str(cfg), "auth"]) == 0
        out = capsys.readouterr().out
        assert "found via $GH_TOKEN" in out and "write on a private repo" in out and "4990 of 5000" in out
        assert "supersecret" not in out
    finally:
        fake.close()


def test_bad_config_is_a_clean_error(tmp_path, capsys, no_token):
    cfg = tmp_path / "greenlight.toml"
    cfg.write_text("[nope]\nx = 1\n")
    assert cli.main(["--config", str(cfg), "flaky"]) == 3
    assert "unknown section" in capsys.readouterr().err


def test_demo_seeds_and_never_touches_a_real_db(tmp_path, capsys, no_token):
    demo = tmp_path / "demo.db"
    assert cli.main(["demo", "--db", str(demo), "--days", "5"]) == 0
    assert "next: greenlight --db" in capsys.readouterr().out
    assert cli.main(["demo", "--db", str(demo), "--days", "5"]) == 0  # reseeding a demo DB is fine
    real = tmp_path / "real.db"
    from greenlight.db import connect
    from greenlight.ingest import TestResult, record_run
    with connect(str(real)) as conn:
        record_run(conn, [TestResult("a::b", None, "pass", 1)], commit_sha="abc", source="local")
    assert cli.main(["demo", "--db", str(real)]) == 3
    assert "already holds real runs" in capsys.readouterr().err
