"""A small GitHub REST client on the standard library.

Auth: a token from $GREENLIGHT_GITHUB_TOKEN, $GH_TOKEN or $GITHUB_TOKEN (in that order), else
`gh auth token` if the GitHub CLI is logged in. greenlight never stores it: not in the DB, the
config file or any output. Public repos can be read without one, at 60 requests an hour.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Iterator

from . import __version__

TOKEN_VARS = ("GREENLIGHT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
MAX_DOWNLOAD = 64 * 1024 * 1024


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    pass


# Desktop apps on macOS start with a bare PATH, so a server they launch can miss Homebrew's gh.
_GH_FALLBACKS = [Path("/opt/homebrew/bin/gh"), Path("/usr/local/bin/gh"), Path("/home/linuxbrew/.linuxbrew/bin/gh")]


def resolve_token() -> tuple[str | None, str]:
    """(token, where it came from). The source is safe to print; the token is not."""
    for var in TOKEN_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value, f"${var}"
    gh = shutil.which("gh") or next((str(p) for p in _GH_FALLBACKS if p.is_file()), None)
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL)  # never the MCP server's protocol pipe
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "gh auth token"
        except (OSError, subprocess.SubprocessError):
            pass
    return None, "none"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class GitHub:
    def __init__(self, repo: str, token: str | None = None, api_url: str = "https://api.github.com",
                 timeout: float = 30, retries: int = 2):
        if not _REPO.match(repo or ""):
            raise ValueError(f"repo must look like owner/name, got {repo!r}")
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
        https = urllib.request.HTTPSHandler(context=ctx)
        self._opener = urllib.request.build_opener(https)
        self._raw = urllib.request.build_opener(https, _NoRedirect)

    # ---------- plumbing ----------
    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if path.startswith("http"):
            url = path
        elif path.startswith("/repos/") or path.startswith("/rate_limit") or path.startswith("/user"):
            url = self.api_url + path
        else:
            url = f"{self.api_url}/repos/{self.repo}{path}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + query
        return url

    def _headers(self, accept: str = "application/vnd.github+json", auth: bool = True) -> dict[str, str]:
        h = {"Accept": accept, "User-Agent": f"greenlight/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None,
                accept: str = "application/vnd.github+json", redirect: bool = True) -> Response:
        url = self._url(path, params)
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers(accept)
        if data is not None:
            headers["Content-Type"] = "application/json"
        opener = self._opener if redirect else self._raw
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            self.calls += 1
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    return Response(r.status, r.headers, r.read(MAX_DOWNLOAD + 1))
            except urllib.error.HTTPError as e:
                resp = Response(e.code, e.headers, e.read(65536) if e.fp else b"")
                if e.code in (301, 302, 303, 307, 308) and not redirect:
                    return resp
                wait = self._retry_wait(resp, attempt)
                if wait is not None:
                    time.sleep(wait)
                    continue
                raise self._error(method, url, resp) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                reason = getattr(e, "reason", e)
                raise GitHubError(f"Could not reach {self.api_url}: {reason}") from None
        raise GitHubError(f"{method} {url} failed after {self.retries + 1} attempts")

    def _retry_wait(self, resp: Response, attempt: int) -> float | None:
        if attempt >= self.retries:
            return None
        if resp.status in (502, 503, 504):
            return 1.0 + attempt
        retry_after = resp.headers.get("Retry-After") if resp.headers else None
        if resp.status in (403, 429) and retry_after and retry_after.isdigit() and int(retry_after) <= 60:
            return float(retry_after)  # secondary rate limit: GitHub says how long
        return None

    def _error(self, method: str, url: str, resp: Response) -> GitHubError:
        try:
            detail = (resp.json() or {}).get("message", "")
        except ValueError:
            detail = resp.body[:200].decode(errors="replace")
        where = url.replace(self.api_url, "")
        h = resp.headers or {}
        if resp.status in (403, 429) and h.get("X-RateLimit-Remaining") == "0":
            reset = h.get("X-RateLimit-Reset")
            when = time.strftime("%H:%M UTC", time.gmtime(int(reset))) if reset and reset.isdigit() else "later"
            hint = "" if self.token else " Set a token (see `greenlight auth`) to get 5,000 requests an hour."
            return RateLimited(f"GitHub rate limit reached; it resets at {when}.{hint}", resp.status)
        if resp.status == 401:
            return GitHubError("GitHub rejected the token (401). It may be expired or revoked. Run `greenlight auth`.", 401)
        if resp.status == 404:
            return GitHubError(f"GitHub returned 404 for {method} {where}. If the repo is private, the token "
                               "needs access to it.", 404)
        if resp.status == 403:
            return GitHubError(f"GitHub refused {method} {where} (403): {detail}. The token may lack a permission "
                               "this needs; `greenlight auth` lists them.", 403)
        return GitHubError(f"GitHub {resp.status} for {method} {where}: {detail}", resp.status)

    # ---------- verbs ----------
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params).json()

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body).json()

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body=body).json()

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body=body).json()

    def paginate(self, path: str, params: dict[str, Any] | None = None, key: str | None = None,
                 limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Follow Link rel=next. `key` picks the list out of wrapped responses ({"workflow_runs": [...]})."""
        params = {"per_page": 100, **(params or {})}
        url: str | None = self._url(path, params)
        seen = 0
        while url:
            resp = self.request("GET", url)
            data = resp.json()
            items = data.get(key, []) if key and isinstance(data, dict) else data
            for item in items or []:
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            m = _NEXT.search(resp.headers.get("Link", "") if resp.headers else "")
            url = m.group(1) if m else None

    def download(self, path: str) -> bytes:
        """Fetch a file that GitHub serves by redirect (Actions artifacts, logs). The token goes to
        GitHub only; the redirect target gets a plain request."""
        resp = self.request("GET", path, redirect=False)
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise GitHubError("GitHub redirected without a Location header")
            req = urllib.request.Request(location, headers={"User-Agent": f"greenlight/{__version__}"})
            try:
                with self._opener.open(req, timeout=self.timeout * 4) as r:
                    body = r.read(MAX_DOWNLOAD + 1)
            except urllib.error.URLError as e:
                raise GitHubError(f"Download failed: {getattr(e, 'reason', e)}") from None
        else:
            body = resp.body
        if len(body) > MAX_DOWNLOAD:
            raise GitHubError(f"Download is larger than {MAX_DOWNLOAD // (1024 * 1024)} MB; skipped")
        return body


def client(repo: str | None, api_url: str = "https://api.github.com", require_token: bool = False) -> GitHub:
    if not repo:
        raise ValueError("No GitHub repo configured. Set [github] repo in greenlight.toml, or run inside a clone "
                         "whose origin is on github.com.")
    token, _ = resolve_token()
    if require_token and not token:
        raise ValueError("This needs a GitHub token. Run `greenlight auth` to see how to set one.")
    return GitHub(repo, token, api_url)
