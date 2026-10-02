"""Local dashboard: `flakewatch ui` serves it, `flakewatch ui --export file.html` writes a read-only snapshot.

Binds to 127.0.0.1 only. Requests with a foreign Host header are rejected (DNS rebinding), and
writes need an X-Flakewatch header, which a cross-site form or simple fetch cannot send.
"""
from __future__ import annotations

import json
import sqlite3
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import analysis, dashboard
from .db import connect, iso, utcnow

UI_FILE = Path(__file__).with_name("ui") / "index.html"
SNAPSHOT_TAG = '<script id="snapshot" type="application/json">null</script>'
Params = dict[str, str]


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
    "/api/test/forecast": lambda c, p: _forecast().test_duration_forecast(
        c, analysis.resolve_test_id(c, _require(p, "id"))),
    "/api/trends/durations": lambda c, p: _forecast().duration_regressions(c, include_series=30),
    "/api/trends/suite": lambda c, p: _forecast().suite_forecast(
        c, p.get("metric", "reruns"), history_days=60, lookback_days=90),
}

POST_ROUTES: dict[str, Callable[[sqlite3.Connection, dict], Any]] = {
    "/api/quarantine": lambda c, b: {"quarantined": analysis.quarantine(
        c, _require(b, "test_id"), b.get("reason") or "quarantined from the dashboard")},
    "/api/unquarantine": lambda c, b: {"removed": analysis.unquarantine(c, _require(b, "test_id"))},
    "/api/sweep": lambda c, b: analysis.sweep(c, apply=bool(b.get("apply"))),
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


def make_handler(db: str | None, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._json(403, {"error": "Host not allowed"})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            route = GET_ROUTES.get(url.path)
            if route is None:
                self._json(404, {"error": f"No route {url.path}"})
                return
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._json(*call(route, db, params, readonly=True))

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return
            if self.headers.get("X-Flakewatch") != "1":
                self._json(403, {"error": "Missing X-Flakewatch header"})
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


def serve(db: str | None, port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"flakewatch ui on {url}  (Ctrl+C to stop)")
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
    tests and runs the list views link to."""
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

    test_ids = {t["test_id"] for t in flaky.get("tests", [])}
    test_ids |= {t["test_id"] for t in ov.get("field", {}).get("tests", [])}
    test_ids |= {q["test_id"] for q in quar.get("quarantined", [])}
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
        grab("/api/test", {"id": tid, "days": d})
        if toto_ok:
            grab("/api/test/forecast", {"id": tid})

    payload = {"generated_at": iso(utcnow()), "days": days, "responses": snap}
    blob = json.dumps(payload, default=str, separators=(",", ":")).replace("</", "<\\/")
    html = UI_FILE.read_text(encoding="utf-8")
    if SNAPSHOT_TAG not in html:
        raise RuntimeError("ui/index.html is missing the snapshot placeholder")
    html = html.replace(SNAPSHOT_TAG, f'<script id="snapshot" type="application/json">{blob}</script>')
    Path(out).write_text(html, encoding="utf-8")
    return {"responses": len(snap), "skipped": skipped}
