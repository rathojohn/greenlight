import http.client
import json
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from greenlight import web

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("demo") / "demo.db"
    subprocess.run([sys.executable, "-m", "greenlight.demo", "--db", str(db), "--days", "21"],
                   check=True, capture_output=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    return str(db)


def start(db, token=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(db, 0, token))
    port = server.server_address[1]
    server.RequestHandlerClass = web.make_handler(db, port, token)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server, port


def req(port, method, path, host=None, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, dict(r.getheaders()), data


@pytest.mark.parametrize("path", ["/api/overview?days=30", "/api/flaky?days=30", "/api/runs?limit=5", "/api/quarantine?days=30",
                                  "/api/pipelines?days=30", "/api/delivery?days=30", "/api/run?id=1",
                                  "/api/pipeline?id=gha:1000:1"])
def test_every_view_answers(demo_db, path):
    server, port = start(demo_db)
    try:
        status, _, data = req(port, "GET", path)
        assert status == 200, data[:300]
        assert json.loads(data)
    finally:
        server.shutdown()


def test_loopback_mode_rejects_foreign_hosts_and_unmarked_writes(demo_db):
    server, port = start(demo_db)
    try:
        assert req(port, "GET", "/api/overview", host="evil.example:80")[0] == 403
        assert req(port, "POST", "/api/sweep", body={})[0] == 403
        status, _, data = req(port, "POST", "/api/sweep", headers={"X-Greenlight": "1", "Content-Type": "application/json"}, body={})
        assert status == 200 and "to_quarantine" in json.loads(data)
    finally:
        server.shutdown()


def test_token_mode(demo_db):
    server, port = start(demo_db, token="s3cret")
    try:
        assert req(port, "GET", "/api/overview")[0] == 401
        assert req(port, "GET", "/")[0] == 401
        assert req(port, "GET", "/?token=wrong")[0] == 401
        status, headers, _ = req(port, "GET", "/?token=s3cret", host="192.168.1.20:8765")
        assert status == 303 and headers["Location"] == "/"
        cookie = headers["Set-Cookie"]
        assert cookie.startswith("greenlight_token=s3cret;") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        ok = {"Cookie": "greenlight_token=s3cret"}
        assert req(port, "GET", "/api/overview", host="192.168.1.20:8765", headers=ok)[0] == 200
        assert req(port, "GET", "/api/overview", headers={"Authorization": "Bearer s3cret"})[0] == 200
        # the token doesn't replace the write header
        assert req(port, "POST", "/api/sweep", headers=ok, body={})[0] == 403
    finally:
        server.shutdown()


def test_snapshot_export_covers_the_new_views(demo_db, tmp_path):
    out = tmp_path / "snap.html"
    res = web.export_snapshot(demo_db, str(out), days=30, with_forecasts=False)
    html = out.read_text()
    assert res["responses"] > 20
    for key in ("/api/pipelines?days=30", "/api/delivery?days=30", "/api/pipeline?id=gha:"):
        assert key in html
    assert "</script><script" not in html.split('id="snapshot"')[1][:200]
