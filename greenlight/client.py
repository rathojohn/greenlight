"""Talk to a hosted greenlight (`greenlight serve`): send runs, get the gate's decision back.

Set GREENLIGHT_URL (the server's address) and GREENLIGHT_TOKEN. `greenlight run`, `greenlight playtest
gate` and the GitHub Action then record into a throwaway database, send the run to the server, and
print the decision it makes against all the history it holds. Standard library only, like the CLI.
"""
from __future__ import annotations

import gzip
import json
import os
import urllib.error
import urllib.request
from typing import Any


class ServerError(RuntimeError):
    pass


def configured() -> tuple[str, str] | None:
    url = (os.environ.get("GREENLIGHT_URL") or "").strip().rstrip("/")
    if not url:
        return None
    token = (os.environ.get("GREENLIGHT_TOKEN") or "").strip()
    if not token:
        raise ServerError("GREENLIGHT_URL is set but GREENLIGHT_TOKEN isn't: the server needs its token")
    return url, token


def submit(records: list[dict[str, Any]], gate: str | None = None, window_days: int | None = None,
           timeout: float = 120) -> dict[str, Any]:
    """POST run records to /api/records. With gate, the reply carries that run's triage."""
    conf = configured()
    if not conf:
        raise ServerError("set GREENLIGHT_URL and GREENLIGHT_TOKEN to send runs to a greenlight server")
    url, token = conf
    body = gzip.compress(json.dumps({"records": records, "gate": gate, "window_days": window_days},
                                    separators=(",", ":"), default=str).encode())
    req = urllib.request.Request(f"{url}/api/records", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Content-Encoding": "gzip",
        "Accept": "application/json", "User-Agent": "greenlight"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()
        try:
            detail = json.loads(detail).get("error") or detail
        except ValueError:
            pass
        hint = " (check GREENLIGHT_TOKEN)" if e.code == 401 else ""
        raise ServerError(f"{url} answered {e.code}: {detail[:300]}{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ServerError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from None
