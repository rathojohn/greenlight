"""Local dashboard: `greenlight ui` serves it, `greenlight ui --export file.html` writes a read-only snapshot.

By default it binds to 127.0.0.1 and needs no login: requests with a foreign Host header are rejected
(DNS rebinding), and writes need an X-Greenlight header, which a cross-site form or simple fetch
cannot send. To open it from a phone on your LAN or tailnet, bind elsewhere with --host; then every
request needs the access token too (printed once as a link that sets a cookie).
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import analysis, dashboard, delivery, usage
from .db import connect, iso, utcnow

UI_FILE = Path(__file__).with_name("ui") / "index.html"
SNAPSHOT_TAG = '<script id="snapshot" type="application/json">null</script>'
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
COOKIE = "greenlight_token"
Params = dict[str, str]
_incident_labels: list[str] | None = None


def set_incident_labels(labels: list[str] | None) -> None:
    global _incident_labels
    _incident_labels = labels


def _int(p: Params, key: str, default: int, lo: int = 1, hi: int = 365) -> int:
    try:
        return max(lo, min(hi, int(p.get(key, default))))
    except ValueError:
        raise ValueError(f"'{key}' must be a whole number")


def _require(p: Params, key: str) -> str:
    if not p.get(key):
        raise ValueError(f"missing '{key}'")
    return p[key]


def _forecast():
    from . import forecast  # torch is slow to import; only load on the Trends views
    return forecast


GET_ROUTES: dict[str, Callable[[sqlite3.Connection, Params], Any]] = {
    "/api/overview": lambda c, p: dashboard.overview(c, _int(p, "days", 30)),
    "/api/flaky": lambda c, p: dashboard.flaky_table(c, _int(p, "days", 30)),
    "/api/test": lambda c, p: dashboard.test_detail(c, _require(p, "id"), _int(p, "days", 30)),
    "/api/runs": lambda c, p: dashboard.runs_list(c, _int(p, "limit", 50, hi=500)),
    "/api/run": lambda c, p: analysis.triage_run(c, run_id=_int(p, "id", 0, lo=0, hi=10**12)),
    "/api/quarantine": lambda c, p: dashboard.quarantine_view(c, _int(p, "days", 30)),
    "/api/pipelines": lambda c, p: delivery.pipelines_summary(c, _int(p, "days", 30)),
    "/api/pipeline": lambda c, p: delivery.pipeline_detail(c, _require(p, "id")),
    "/api/delivery": lambda c, p: delivery.delivery_view(c, _int(p, "days", 30), _incident_labels, p.get("env") or None),
    "/api/test/forecast": lambda c, p: _forecast().test_duration_forecast(
        c, analysis.resolve_test_id(c, _require(p, "id"))),
    "/api/trends/durations": lambda c, p: _forecast().duration_regressions(c, include_series=30),
    "/api/usage": lambda c, p: usage.summary(c, _int(p, "days", 30)),
    "/api/usage/detail": lambda c, p: usage.detail(c, _int(p, "days", 30), _require(p, "kind"), _require(p, "key")),
    "/api/session": lambda c, p: {"auth": "local"},  # the hosted server answers this itself (cookie, bearer...)
    "/api/trends/suite": lambda c, p: _forecast().suite_forecast(
        c, p.get("metric", "reruns"), history_days=60, lookback_days=90),
}

POST_ROUTES: dict[str, Callable[[sqlite3.Connection, dict], Any]] = {
    "/api/quarantine": lambda c, b: {"quarantined": analysis.quarantine(
        c, _require(b, "test_id"), b.get("reason") or "quarantined from the dashboard")},
    "/api/unquarantine": lambda c, b: {"removed": analysis.unquarantine(c, _require(b, "test_id"))},
    "/api/sweep": lambda c, b: analysis.sweep(c, apply=bool(b.get("apply"))),
    "/api/usage": lambda c, b: usage.store(c, b),
    "/api/forget": lambda c, b: {"forgotten": analysis.forget_runs(c, _require(b, "runs"), bool(b.get("dry_run")))},
}

ERRORS = [(LookupError, 404), (ValueError, 400), (FileNotFoundError, 503), (RuntimeError, 501)]


def call(route: Callable, db: str | None, params: Any, readonly: bool) -> tuple[int, Any]:
    try:
        with closing(connect(db, readonly=readonly)) as conn:
            return 200, route(conn, params)
    except sqlite3.Error as e:
        return 500, {"error": f"SQLite: {e}"}
    except Exception as e:  # noqa: BLE001 - map known types, surface the rest
        for kind, status in ERRORS:
            if isinstance(e, kind):
                return status, {"error": str(e)}
        return 500, {"error": f"{type(e).__name__}: {e}"}


LOGIN_PAGE = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>greenlight</title><body style="font:16px system-ui;max-width:34rem;margin:15vh auto;padding:0 16px;
background:#F1F3F5;color:#16202A"><h1 style="font-size:1.4rem">greenlight needs its access link</h1>
<p>Open the link <code>greenlight ui</code> printed when it started. It ends in <code>?token=</code> and signs this
browser in.</p></body>"""


def make_handler(db: str | None, port: int, token: str | None = None) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _allowed(self) -> bool:
            """Loopback mode trusts only local Host headers. Token mode trusts the token instead: a page on
            another origin never has the cookie, and can't read the token out of this one."""
            if token is None:
                if self.headers.get("Host") in allowed_hosts:
                    return True
                self._json(403, {"error": "Host not allowed"})
                return False
            given = self._token()
            if given and hmac.compare_digest(given.encode(), token.encode()):
                return True
            if urlparse(self.path).path.startswith("/api/"):
                self._json(401, {"error": "Missing or wrong access token"})
            else:
                self._send(401, LOGIN_PAGE, "text/html; charset=utf-8")
            return False

        def _token(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if token is not None and url.path == "/" and "token" in query:
                if hmac.compare_digest(query["token"].encode(), token.encode()):
                    # trade the link for a cookie, and drop the token from the address bar and history
                    self._send(303, b"", "text/plain", {
                        "Location": "/", "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
                else:
                    self._send(401, LOGIN_PAGE, "text/html; charset=utf-8")
                return
            if not self._allowed():
                return
            if url.path in ("/", "/index.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            route = GET_ROUTES.get(url.path)
            if route is None:
                self._json(404, {"error": f"No route {url.path}"})
                return
            self._json(*call(route, db, query, readonly=True))

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            if self.headers.get("X-Greenlight") != "1":
                self._json(403, {"error": "Missing X-Greenlight header"})
                return
            route = POST_ROUTES.get(urlparse(self.path).path)
            if route is None:
                self._json(404, {"error": "No such action"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "Body must be JSON"})
                return
            self._json(*call(route, db, body, readonly=False))

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def serve(db: str | None, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1",
          token: str | None = None) -> None:
    remote = host not in LOOPBACK
    if remote and not token:
        token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(db, port, token if remote else None))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/" + (f"?token={token}" if remote else "")
    if remote:
        print(f"greenlight ui on {host}:{port}. Open this once on each device (it signs the browser in):\n  {url}")
        print("Use it on a network you trust (home LAN, Tailscale). It is plain HTTP.  (Ctrl+C to stop)")
    else:
        print(f"greenlight ui on {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def snap_key(path: str, params: Params) -> str:
    """Must match snapKey() in ui/index.html: raw values, keys sorted, no URL encoding."""
    return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def export_snapshot(db: str | None, out: str, days: int = 30, with_forecasts: bool = True) -> dict[str, int]:
    """Render every view's data into one self-contained HTML file. Detail pages are included for the
    tests, runs and pipelines the list views link to."""
    snap: dict[str, Any] = {}
    skipped = 0

    def grab(path: str, params: Params) -> Any:
        """Errors are stored too, so the snapshot shows the same message the live UI would."""
        nonlocal skipped
        status, data = call(GET_ROUTES[path], db, params, readonly=True)
        if status == 200:
            snap[snap_key(path, params)] = data
            return data
        skipped += 1
        snap[snap_key(path, params)] = {"__status": status, **data}
        return None

    d = str(days)
    ov = grab("/api/overview", {"days": d}) or {}
    flaky = grab("/api/flaky", {"days": d}) or {}
    runs = grab("/api/runs", {"limit": "50"}) or {}
    quar = grab("/api/quarantine", {"days": d}) or {}
    pipes = grab("/api/pipelines", {"days": d}) or {}
    deliv = grab("/api/delivery", {"days": d}) or {}
    grab("/api/usage", {"days": d})
    for env in (deliv.get("dora") or {}).get("environments", []):
        grab("/api/delivery", {"days": d, "env": env["environment"]})
    for p in pipes.get("recent", [])[:20]:
        grab("/api/pipeline", {"id": p["pipeline_id"]})

    test_ids = {t["test_id"] for t in flaky.get("tests", [])}
    test_ids |= {t["test_id"] for t in ov.get("field", {}).get("tests", [])}
    test_ids |= {q["test_id"] for q in quar.get("quarantined", [])}
    test_ids |= {i["test_id"] for i in ((deliv.get("issues") or {}).get("managed") or [])}
    for r in runs.get("runs", [])[:25]:
        t = grab("/api/run", {"id": str(r["run_id"])})
        test_ids |= {f["test_id"] for f in (t or {}).get("failures", [])}

    toto_ok = with_forecasts
    if with_forecasts:
        toto_ok = grab("/api/trends/durations", {}) is not None
        if toto_ok:
            for metric in ("reruns", "failure_rate", "suite_duration_ms", "runs"):
                grab("/api/trends/suite", {"metric": metric})
    for tid in sorted(test_ids):
        if grab("/api/test", {"id": tid, "days": d}) is not None and toto_ok:
            grab("/api/test/forecast", {"id": tid})

    payload = {"generated_at": iso(utcnow()), "days": days, "responses": snap}
    blob = json.dumps(payload, default=str, separators=(",", ":")).replace("</", "<\\/")
    html = UI_FILE.read_text(encoding="utf-8")
    if SNAPSHOT_TAG not in html:
        raise RuntimeError("ui/index.html is missing the snapshot placeholder")
    html = html.replace(SNAPSHOT_TAG, f'<script id="snapshot" type="application/json">{blob}</script>')
    Path(out).write_text(html, encoding="utf-8")
    return {"responses": len(snap), "skipped": skipped}
