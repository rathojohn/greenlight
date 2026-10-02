"""The hosted server (`greenlight serve`) and the clients that send it runs. A real server process on a free
port, its own database, and the CLI pointed at it with GREENLIGHT_URL and GREENLIGHT_TOKEN."""
import gzip
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing

import pytest

from greenlight import ci, cli
from greenlight.db import connect
from tests.conftest import child_env, junit_xml
from tests.test_ci import FLAKY, actions, write_junit  # noqa: F401 - fixture
from tests.test_playtest import game, record, write_record  # noqa: F401 - fixture
from tests.test_setup import checkout  # noqa: F401 - fixture

TOKEN = "hosted-t0ken"


def start_server(tmp_path, stderr=subprocess.DEVNULL):
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = tmp_path / "server.db"
    home = tmp_path / "server-home"
    home.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "greenlight", "serve", "--port", str(port)],
                            env=child_env(GREENLIGHT_DB=str(db), GREENLIGHT_TOKEN=TOKEN), cwd=str(home),
                            stdout=subprocess.DEVNULL, stderr=stderr)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    return proc, base, db


@pytest.fixture
def server(tmp_path):
    proc, base, db = start_server(tmp_path)
    yield base, db
    proc.terminate()
    proc.wait(10)


def test_a_token_you_set_stays_out_of_the_log(tmp_path):
    proc, _, _ = start_server(tmp_path, stderr=subprocess.PIPE)  # hosts keep what a server prints
    proc.terminate()
    log = proc.communicate(timeout=10)[1].decode()
    assert TOKEN not in log and "/$GREENLIGHT_TOKEN/mcp" in log


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def call(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def use_server(monkeypatch, base: str) -> None:
    monkeypatch.setenv("GREENLIGHT_URL", base)
    monkeypatch.setenv("GREENLIGHT_TOKEN", TOKEN)


def test_one_token_three_ways_in(server):
    base, _ = server
    assert call(base + "/healthz")[0] == 200
    status, _, page = call(base + "/", headers={"Accept": "text/html"})
    assert status == 401 and b"Sign in" in page
    assert call(base + "/?token=wrong")[0] == 401
    status, headers, _ = call(base + f"/?token={TOKEN}")
    cookie = headers["Set-Cookie"]
    assert status == 303 and headers["Location"] == "/" and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    jar = {"Cookie": cookie.split(";")[0]}
    status, _, page = call(base + "/", headers=jar)
    assert status == 200 and b"<html" in page.lower()
    assert call(base + "/api/runs", headers=jar)[0] == 200
    assert call(base + "/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert call(base + f"/{TOKEN}/api/runs")[0] == 200
    assert call(base + "/api/runs")[0] == 401
    # a cookie can be sent by any site's form, so a cookie POST needs the header no form can set
    sweep = json.dumps({"apply": False}).encode()
    assert call(base + "/api/sweep", "POST", jar, sweep)[0] == 403
    assert call(base + "/api/sweep", "POST", {**jar, "X-Greenlight": "1"}, sweep)[0] == 200
    assert call(base + "/api/sweep", "POST", {"Authorization": f"Bearer {TOKEN}"}, sweep)[0] == 200


def test_records_are_loaded_once_and_judged(server, tmp_path, actions):  # noqa: F811
    base, db = server
    with connect(str(tmp_path / "job.db")) as conn:
        res = ci.record_junit(conn, [write_junit(tmp_path / "j.xml", [(FLAKY, "pass", 0.5, None)])])
        rec = ci.export_run(conn, res["run_id"])
    auth = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    body = json.dumps({"records": [rec], "gate": rec["external_id"]}).encode()
    status, _, out = call(base + "/api/records", "POST", {**auth, "Content-Encoding": "gzip"}, gzip.compress(body))
    out = json.loads(out)
    assert status == 200 and out["loaded"] == 1 and out["triage"]["decision"] == "PASS"
    out = json.loads(call(base + "/api/records", "POST", auth, body)[2])
    assert out["loaded"] == 0 and out["triage"]["decision"] == "PASS"  # the same record twice adds nothing
    assert call(base + "/api/records", "POST", auth, b'{"records": []}')[0] == 400
    assert call(base + "/api/records", "POST", auth, json.dumps({"records": [rec], "gate": "nope"}).encode())[0] == 400
    with closing(connect(str(db), readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_run_is_judged_by_the_server(server, checkout, monkeypatch, tmp_path, capsys):  # noqa: F811
    base, db = server
    repo, sha = checkout
    monkeypatch.chdir(repo)
    use_server(monkeypatch, base)
    local = tmp_path / "local.db"
    pytest_cmd = ["--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    assert cli.main(["--db", str(local), "run", *pytest_cmd]) == 0
    assert f"on {base}" in capsys.readouterr().out
    monkeypatch.setenv("FLIP", "1")  # same code, other result: the server has both runs, so it's a flip
    assert cli.main(["--db", str(local), "run", *pytest_cmd]) == 2
    assert "RERUN_TARGETED" in capsys.readouterr().out
    assert not local.exists()  # nothing kept on this machine
    with closing(connect(str(db), readonly=True)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT commit_sha, attempt FROM runs ORDER BY run_id")] == \
            [(sha, 1), (sha, 2)]


def test_playtest_gate_is_judged_by_the_server(server, game, monkeypatch, tmp_path, capsys):  # noqa: F811
    base, db = server
    repo, head = game
    use_server(monkeypatch, base)
    check_id = "the cart total includes tax"
    write_record(repo, record("2026-10-02T05:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [check_id]}}, changed={"b": "2"}))
    write_record(repo, record("2026-10-02T05:10:00.000Z", head, {"smoke": {"pass": True, "checks": 38}},
                              changed={"b": "2"}))
    write_record(repo, record("2026-10-02T06:00:00.000Z", head, {
        "smoke": {"pass": False, "checks": 38, "failed": [check_id]}}, changed={"c": "3"}))
    assert cli.main(["--db", str(tmp_path / "local.db"), "playtest", "gate", "--repo", str(repo)]) == 2
    assert "rerun only: node tools/playtest/run.cjs smoke --rerun" in capsys.readouterr().out
    with closing(connect(str(db), readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE source = 'playtest'").fetchone()[0] >= 3


def test_ci_report_gets_the_decision_from_the_server(server, actions, monkeypatch, tmp_path, capsys):  # noqa: F811
    base, _ = server
    use_server(monkeypatch, base)
    for attempt, outcome, expected in (("1", "pass", 0), ("2", "fail", 0)):
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
        job_db = str(tmp_path / f"job{attempt}.db")  # every job starts with an empty disk
        report = write_junit(tmp_path / f"r{attempt}.xml", [(FLAKY, outcome, 1.0, "boom" if outcome == "fail" else None),
                                                           ("t.a::ok", "pass", 0.1, None)])
        assert cli.main(["--db", job_db, "ci", "record", "--junit", report]) == 0
        assert cli.main(["--db", job_db, "ci", "report"]) == expected
    summary = (actions / "summary.md").read_text()
    assert "Rerun the flaky tests only" in summary  # attempt 2 failed what attempt 1 passed, on the same commit
    assert "decision=RERUN_TARGETED" in (actions / "out.txt").read_text()


def test_otlp_spans_land_on_the_server(server):
    base, db = server
    span = {"traceId": "a" * 32, "spanId": "b" * 16, "name": "test", "startTimeUnixNano": "1790000000000000000",
            "endTimeUnixNano": "1790000001000000000",
            "attributes": [{"key": "test.case.name", "value": {"stringValue": "t.x::y"}},
                           {"key": "test.case.result.status", "value": {"stringValue": "fail"}}]}
    payload = json.dumps({"resourceSpans": [{"resource": {"attributes": [
        {"key": "vcs.ref.head.revision", "value": {"stringValue": "f" * 40}}]},
        "scopeSpans": [{"spans": [span]}]}]}).encode()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert call(base + "/v1/traces", "POST", {**auth, "Content-Type": "text/plain"}, payload)[0] == 415
    assert call(base + "/v1/traces", "POST", {**auth, "Content-Type": "application/json"}, payload)[0] == 200
    with closing(connect(str(db), readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM results WHERE test_id = 't.x::y'").fetchone()[0] == 1


def test_sign_in_page_takes_the_token_in_a_form(server):
    base, _ = server
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, _, page = call(base + "/login")
    assert status == 200 and b'type="password"' in page and b'autocomplete="current-password"' in page
    status, _, page = call(base + "/login", "POST", form, b"username=greenlight&token=wrong")
    assert status == 401 and b"doesn't match" in page
    status, headers, _ = call(base + "/login", "POST", form, f"username=greenlight&token={TOKEN}".encode())
    cookie = headers["Set-Cookie"]
    assert status == 303 and headers["Location"] == "/" and "HttpOnly" in cookie and "Max-Age=34560000" in cookie
    jar = {"Cookie": cookie.split(";")[0]}
    assert call(base + "/", headers=jar)[0] == 200
    assert json.loads(call(base + "/api/session", headers=jar)[2]) == {"auth": "cookie"}
    assert json.loads(call(base + "/api/session", headers={"Authorization": f"Bearer {TOKEN}"})[2]) == {"auth": "bearer"}
    status, headers, _ = call(base + "/logout", "POST", jar)
    assert status == 303 and headers["Location"] == "/login" and "Max-Age=0" in headers["Set-Cookie"]


def test_ui_opens_the_server_with_a_one_time_link(server, monkeypatch, tmp_path, capsys):
    base, _ = server
    use_server(monkeypatch, base)
    assert cli.main(["ui", "--no-browser"]) == 0
    link = capsys.readouterr().out.strip().split()[-1]
    assert link.startswith(base + "/login?code=") and TOKEN not in link
    status, headers, _ = call(link)
    assert status == 303 and headers["Set-Cookie"].startswith(f"greenlight_token={TOKEN};")
    status, _, page = call(link)  # it works once
    assert status == 401 and b"already used" in page
    assert call(base + "/api/login-code", "POST", {"Content-Type": "application/json"}, b"{}")[0] == 401
    monkeypatch.setattr("webbrowser.open", lambda url: (_ for _ in ()).throw(AssertionError("opened a browser")))
    assert cli.main(["--db", str(tmp_path / "x.db"), "ui", "--export", str(tmp_path / "s.html")]) == 0  # still local


def test_forget_deletes_only_the_runs_named(server, checkout, monkeypatch, tmp_path, capsys):  # noqa: F811
    base, db = server
    repo, _ = checkout
    monkeypatch.chdir(repo)
    use_server(monkeypatch, base)
    pytest_cmd = ["--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    for _ in range(2):
        assert cli.main(["--db", str(tmp_path / "l.db"), "run", *pytest_cmd]) == 0
    with closing(connect(str(db), readonly=True)) as conn:
        first, second = [r[0] for r in conn.execute("SELECT external_id FROM runs ORDER BY run_id")]
    capsys.readouterr()
    assert cli.main(["forget", "--dry-run", "1", "nope"]) == 0
    assert "would forget run 1" in capsys.readouterr().out
    assert cli.main(["forget", "1", second]) == 0
    out = capsys.readouterr().out
    assert "forgot run 1" in out and "forgot run 2" in out and "2 of 2 runs forgot on" in out
    with closing(connect(str(db), readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0  # results went with them
    assert first  # named by number above


def test_the_usage_hook_reports_to_the_server(server, monkeypatch, tmp_path):
    import io
    base, db = server
    use_server(monkeypatch, base)
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-02T10:00:00Z", "gitBranch": "fix",
                               "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
                                   "input_tokens": 3, "output_tokens": 70, "cache_read_input_tokens": 900}}}) + "\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "transcript_path": str(log)})))
    assert cli.main(["usage", "record", "--hook"]) == 0
    with closing(connect(str(db), readonly=True)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT session_id, branch, output_tokens FROM agent_usage")] == [("s1", "fix", 70)]
    status, _, body = call(base + "/api/usage?days=3650", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and json.loads(body)["totals"]["output_tokens"] == 70
