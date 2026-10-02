"""An in-process stand-in for the GitHub REST API, enough for flakewatch's client and sync."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

Handler = Callable[[dict[str, str], Any], tuple[int, Any] | tuple[int, Any, dict[str, str]]]


class FakeGitHub:
    def __init__(self, repo: str = "o/r"):
        self.repo = repo
        self.routes: list[tuple[str, re.Pattern, Handler]] = []
        self.requests: list[dict[str, Any]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def route(self, method: str, path: str, handler: Handler | Any) -> None:
        """path is a regex over the URL path, with {repo} expanded. A non-callable is returned as JSON."""
        pattern = re.compile("^" + path.replace("{repo}", f"/repos/{re.escape(self.repo)}") + "$")
        fn = handler if callable(handler) else (lambda q, b, v=handler: (200, v))
        self.routes.insert(0, (method, pattern, fn))

    def pages(self, method: str, path: str, items: list[Any], per_page: int = 2, key: str | None = None) -> None:
        """Serve `items` in pages linked with rel="next", like GitHub."""
        def handler(q, _b, items=items):
            page = int(q.get("page", "1"))
            chunk = items[(page - 1) * per_page: page * per_page]
            headers = {}
            if page * per_page < len(items):
                base = f"{self.url}{q['__path']}"
                rest = {k: v for k, v in q.items() if not k.startswith("__") and k != "page"}
                qs = "&".join(f"{k}={v}" for k, v in {**rest, "page": page + 1}.items())
                headers["Link"] = f'<{base}?{qs}>; rel="next"'
            body = {key: chunk, "total_count": len(items)} if key else chunk
            return 200, body, headers
        self.route(method, path, handler)

    def calls(self, method: str, prefix: str) -> list[dict[str, Any]]:
        p = prefix.replace("{repo}", f"/repos/{self.repo}")
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(p)]

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def _do(self, method: str) -> None:
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                q["__path"] = url.path
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append({"method": method, "path": url.path, "query": q, "body": body,
                                      "headers": dict(self.headers)})
                for m, pattern, fn in fake.routes:
                    if m == method and pattern.match(url.path):
                        out = fn(q, body)
                        status, data = out[0], out[1]
                        headers = out[2] if len(out) > 2 else {}
                        break
                else:
                    status, data, headers = 404, {"message": "Not Found"}, {}
                raw = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self._do("GET")

            def do_POST(self):  # noqa: N802
                self._do("POST")

            def do_PATCH(self):  # noqa: N802
                self._do("PATCH")

            def do_PUT(self):  # noqa: N802
                self._do("PUT")

            def log_message(self, *a):
                pass

        return H
