"""`greenlight serve`: one HTTP server for everything a hosted greenlight does.

  /mcp          MCP over streamable HTTP, for Claude Code, Codex, claude.ai and ChatGPT
  /             the dashboard, with /api/* behind it
  /api/records  runs sent by `greenlight run`, `greenlight playtest gate` and the GitHub Action; the reply
                is the gate's decision, judged against everything the server has seen
  /v1/traces    OTLP/HTTP JSON test and CI/CD spans
  /healthz      for the host's health check, the only path without the token

One token guards the rest (GREENLIGHT_TOKEN): an `Authorization: Bearer` header (the CLI, CI, Claude Code,
Codex), the URL path `/<token>/...` (claude.ai and ChatGPT connectors can't send headers), or the cookie a
browser gets by opening `/?token=<token>` once.
"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import secrets
import sqlite3
import sys
import zlib
from contextlib import closing
from typing import Any
from urllib.parse import parse_qs

from . import analysis, ci, otel, server, web
from .db import connect

MAX_BODY = 32 * 1024 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "::1")
HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
           (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]
LOGIN_PAGE = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>greenlight</title><body style="font:15px system-ui;max-width:32rem;margin:15vh auto;padding:0 16px;
background:#0E1116;color:#D8DEE6"><h1 style="font-size:1.2rem">Sign in</h1>
<p>Open this address once with your token: <code>/?token=&lt;GREENLIGHT_TOKEN&gt;</code>. The browser keeps a
cookie after that.</p></body>"""


def _token() -> str | None:
    return os.environ.get("GREENLIGHT_TOKEN") or os.environ.get("GREENLIGHT_MCP_TOKEN") or None


def _same(given: str | None, token: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), token.encode())


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


async def _reply(send: Any, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                 headers: list | None = None) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
                            *HEADERS, *(headers or [])]})
    await send({"type": "http.response.body", "body": body})


def _login_cookie(scope: dict, token: str) -> list:
    secure = "; Secure" if (_header(scope, b"x-forwarded-proto") == "https" or scope.get("scheme") == "https") else ""
    # Lax so the link works when opened from a chat or an email; POSTs still need X-Greenlight (see api_post)
    return [(b"set-cookie", f"{web.COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}".encode()),
            (b"location", b"/")]


class Guard:
    """The token check in front of every route. It records how a request got in (scope["greenlight.auth"]):
    a cookie-authenticated POST must also carry X-Greenlight, which a cross-site form can't send."""

    def __init__(self, app: Any, token: str | None, loopback: bool):
        self.app = app
        self.token = token
        self.loopback = loopback

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan: the MCP session manager starts and stops here
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if path == "/healthz":
            return await _reply(send, 200, b"ok\n")
        if self.token is None:  # no token only on loopback (or --no-auth): refuse foreign Host headers there
            host = (_header(scope, b"host") or "").rsplit(":", 1)[0].strip("[]")
            if self.loopback and host not in LOOPBACK:
                return await _reply(send, 403, b"greenlight: Host not allowed\n")
            return await self.app({**scope, "greenlight.auth": "open"}, receive, send)
        token = self.token
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        if path == "/" and "token" in query:  # the sign-in link: trade it for a cookie, drop it from the address
            if _same(query["token"][0], token):
                return await _reply(send, 303, b"", headers=_login_cookie(scope, token))
            return await _reply(send, 401, LOGIN_PAGE, "text/html; charset=utf-8")
        first, _, rest = path.lstrip("/").partition("/")
        if _same(first, token):
            if not rest:  # /<token> opened in a browser signs it in too
                return await _reply(send, 303, b"", headers=_login_cookie(scope, token))
            scope = {**scope, "path": "/" + rest, "raw_path": ("/" + rest).encode(), "greenlight.auth": "path"}
        else:
            auth = _header(scope, b"authorization") or ""
            kind, _, given = auth.partition(" ")
            cookie = next((v for k, _, v in (p.strip().partition("=") for p in (_header(scope, b"cookie") or "").split(";"))
                           if k == web.COOKIE), None)
            if kind.lower() == "bearer" and _same(given.strip(), token):
                scope = {**scope, "greenlight.auth": "bearer"}
            elif _same(cookie, token):
                scope = {**scope, "greenlight.auth": "cookie"}
            elif "text/html" in (_header(scope, b"accept") or ""):
                return await _reply(send, 401, LOGIN_PAGE, "text/html; charset=utf-8")
            else:
                return await _reply(send, 401, b"greenlight: missing or wrong token\n",
                                    headers=[(b"www-authenticate", b"Bearer")])
        return await self.app(scope, receive, send)


def _json(status: int, body: Any) -> Any:
    from starlette.responses import Response
    return Response(json.dumps(body, default=str), status_code=status, media_type="application/json",
                    headers={k.decode(): v.decode() for k, v in HEADERS})


async def _body(request: Any) -> bytes:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("body larger than 32 MB")
    if (request.headers.get("content-encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def ingest_records(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Load run records (the same format `ci.export_run` writes) and judge one of them. Records are keyed by
    their external id, so sending one twice, or getting it later from a sync, adds nothing."""
    records = data.get("records")
    if not isinstance(records, list) or not records or len(records) > 1000:
        return 400, {"error": "send {\"records\": [...]} with 1 to 1000 run records"}
    gate = data.get("gate")
    with closing(connect()) as conn:
        loaded = sum(1 for r in records if isinstance(r, dict) and ci.load_record(conn, r))
        out: dict[str, Any] = {"received": len(records), "loaded": loaded, "triage": None, "issue_links": {}}
        if gate:
            row = conn.execute("SELECT run_id FROM runs WHERE external_id = ?", (gate,)).fetchone()
            if not row:
                return 400, {"error": f"no run {gate!r} among the records sent or already recorded"}
            window = int(data.get("window_days") or analysis.DEFAULT_WINDOW_DAYS)
            t = analysis.triage_run(conn, run_id=row[0], window_days=window)
            out["triage"] = t
            out["issue_links"] = ci.issue_links(conn, [f["test_id"] for f in t["failures"]])
    return 200, out


def _ingest_otlp(payload: dict[str, Any]) -> dict[str, int]:
    with closing(connect()) as conn:
        return otel.ingest_traces(conn, payload)


_registered = False


def _register_routes() -> None:
    """The dashboard, ingest and OTLP routes, on the same app as /mcp."""
    global _registered
    if _registered:
        return
    _registered = True
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response
    mcp = server.mcp

    @mcp.custom_route("/", methods=["GET"])
    async def page(request: Any) -> Any:
        return Response(web.UI_FILE.read_bytes(), media_type="text/html; charset=utf-8",
                        headers={k.decode(): v.decode() for k, v in HEADERS})

    @mcp.custom_route("/api/{name:path}", methods=["GET"])
    async def api_get(request: Any) -> Any:
        route = web.GET_ROUTES.get(request.url.path)
        if route is None:
            return _json(404, {"error": f"No route {request.url.path}"})
        waiting = await run_in_threadpool(server.not_ready)
        if waiting:
            return _json(503, {"error": waiting})
        return _json(*await run_in_threadpool(web.call, route, None, dict(request.query_params), True))

    @mcp.custom_route("/api/{name:path}", methods=["POST"])
    async def api_post(request: Any) -> Any:
        try:
            raw = await _body(request)
            body = json.loads(raw or b"{}")
        except (ValueError, OSError, EOFError, zlib.error) as e:
            return _json(400, {"error": f"Body must be JSON (gzip is fine): {e}"})
        if not isinstance(body, dict):
            return _json(400, {"error": "Body must be a JSON object"})
        if request.scope.get("greenlight.auth") in ("cookie", "open") and request.headers.get("x-greenlight") != "1":
            return _json(403, {"error": "Missing X-Greenlight header"})
        if request.url.path == "/api/records":
            waiting = await run_in_threadpool(server.not_ready)  # judge against synced history, not an empty DB
            if waiting:
                return _json(503, {"error": waiting})
            try:
                return _json(*await run_in_threadpool(ingest_records, body))
            except (sqlite3.Error, ValueError) as e:
                return _json(400, {"error": str(e)})
        route = web.POST_ROUTES.get(request.url.path)
        if route is None:
            return _json(404, {"error": "No such action"})
        return _json(*await run_in_threadpool(web.call, route, None, body, False))

    @mcp.custom_route("/v1/{signal}", methods=["POST"])
    async def otlp(request: Any) -> Any:
        if request.url.path in ("/v1/metrics", "/v1/logs"):
            return _json(200, {})  # accepted and dropped: greenlight keeps traces only
        if request.url.path != "/v1/traces":
            return _json(404, {"message": f"no route {request.url.path}"})
        if "json" not in (request.headers.get("content-type") or ""):
            return _json(415, {"message": "send OTLP as JSON (otlphttp exporter with encoding: json)"})
        try:
            payload = otel.read_body(await request.body(), request.headers.get("content-encoding"))
            await run_in_threadpool(_ingest_otlp, payload)
        except (ValueError, OSError, zlib.error) as e:
            return _json(400, {"message": str(e)})
        return _json(200, {})


def http_app(token: str | None, host: str = "127.0.0.1") -> Any:
    """Stateless JSON responses for MCP, so it runs behind any proxy or host. On loopback without a token it
    refuses other Host headers (DNS rebinding)."""
    from mcp.server.transport_security import TransportSecuritySettings
    loopback = host in LOOPBACK
    _register_routes()
    if loopback:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                                             allowed_origins=["http://127.0.0.1:*", "http://localhost:*"])
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    try:
        app = server.mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                             transport_security=security, host=host)
    except TypeError as e:  # mcp 1.x takes these as settings
        raise RuntimeError("greenlight serve needs mcp 2.2 or newer: run `uv tool upgrade greenlight`") from e
    return Guard(app, token, loopback)


def serve_http(host: str = "127.0.0.1", port: int = 8000, token: str | None = None, no_auth: bool = False) -> None:
    import uvicorn
    token = None if no_auth else (token or _token())
    shown_token = "$GREENLIGHT_TOKEN"  # a token you set stays out of the log: hosts keep logs, and logs get shared
    if not token and not no_auth and host not in LOOPBACK:
        token = shown_token = secrets.token_urlsafe(24)
        print("No GREENLIGHT_TOKEN set, so this run uses a random token. Set one to keep the URLs across "
              "restarts.", file=sys.stderr)
    server._prune_tools()
    with closing(connect()):  # a new server shows an empty dashboard, not "no database yet"
        pass
    if server.STATE.refresher:
        server.STATE.refresher.kick()  # start reading the repo now, not on the first question
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    base = f"http://{shown}:{port}"
    what = server.STATE.repo or server.STATE.cfg.repo or server.STATE.project
    lines = [f"greenlight for {what} on {base}"]
    if token:
        lines += [f"  dashboard   {base}/?token={shown_token}  (once per browser)",
                  f"  MCP         {base}/{shown_token}/mcp  (claude.ai, ChatGPT), or {base}/mcp with Authorization: Bearer",
                  f"  runs        {base}  with GREENLIGHT_URL and GREENLIGHT_TOKEN set for the CLI and the Action"]
    else:
        lines += [f"  dashboard   {base}/", f"  MCP         {base}/mcp"]
    lines.append("Behind your host's HTTPS address, use that address instead of " + base + ".")
    print("\n".join(lines), file=sys.stderr)
    uvicorn.run(http_app(token, host), host=host, port=port, log_level="warning", access_log=False,
                proxy_headers=True, forwarded_allow_ips="*")
