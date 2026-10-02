from __future__ import annotations

import os
import shutil
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greenlight.db import connect  # noqa: E402
from greenlight.ingest import ingest_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_servers(monkeypatch):
    """Whatever this shell has set (a greenlight server, an OTLP backend), tests never send anything there.
    Tests that need a server or a backend set their own."""
    for k in list(os.environ):
        if k.startswith(("GREENLIGHT_", "OTEL_")):
            monkeypatch.delenv(k)


def hide_clis(monkeypatch) -> None:
    """Leave git on PATH and nothing else greenlight shells out to (gh, claude), on any OS."""
    from greenlight import github
    git = shutil.which("git")
    monkeypatch.setenv("PATH", os.path.dirname(git) if git else "")
    monkeypatch.setattr(github, "_GH_FALLBACKS", [])


def child_env(**extra: str) -> dict[str, str]:
    """The environment for a greenlight subprocess: this one, minus anything pointing at real data or
    tokens. Copied rather than built from scratch, since Python won't start on Windows without SYSTEMROOT."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GREENLIGHT_", "GH_", "GITHUB_")) and k != "CLAUDE_PROJECT_DIR"}
    return {**env, "PYTHONPATH": str(ROOT), **extra}


def junit_xml(cases: list[tuple[str, str, float, str | None]]) -> str:
    """cases: (test_id 'cls::name', outcome pass|fail|error|skip, seconds, message)."""
    rows = []
    for test_id, outcome, secs, msg in cases:
        cls, name = test_id.split("::")
        inner = ""
        if outcome == "fail":
            inner = f"<failure message={quoteattr(msg or 'failed')}/>"
        elif outcome == "error":
            inner = f"<error message={quoteattr(msg or 'error')}/>"
        elif outcome == "skip":
            inner = "<skipped/>"
        rows.append(f'<testcase classname="{cls}" name="{name}" time="{secs:.3f}">{inner}</testcase>')
    return f'<?xml version="1.0"?><testsuite name="t">{"".join(rows)}</testsuite>'


class Recorder:
    """Writes JUnit files and ingests them, one call per run."""

    def __init__(self, conn, tmp: Path):
        self.conn = conn
        self.tmp = tmp
        self.n = 0
        self.t0 = datetime.now(timezone.utc) - timedelta(days=5)

    def run(self, sha: str, cases, minutes: int | None = None, **kw) -> int:
        self.n += 1
        path = self.tmp / f"run-{self.n}.xml"
        path.write_text(junit_xml(cases))
        when = self.t0 + timedelta(minutes=minutes if minutes is not None else self.n * 10)
        run_id, _, _ = ingest_files(self.conn, [path], commit_sha=sha, started_at=when, **kw)
        return run_id


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fw.db"
    with closing(connect(str(path))) as conn:
        yield conn, str(path)


@pytest.fixture
def rec(db, tmp_path):
    conn, _ = db
    return Recorder(conn, tmp_path)
