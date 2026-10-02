"""Parse JUnit XML (pytest, jest-junit, playwright, vitest, surefire, go-junit-report...) and record a run.

Retries are picked up two ways:
  1. The same test appears more than once in the report(s) -> each extra entry is a retry.
  2. Surefire-style <flakyFailure>/<rerunFailure> children -> earlier failed attempts.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import iso, utcnow

MESSAGE_LIMIT = 500
_RETRY_TAGS = {"flakyFailure": "fail", "flakyError": "error", "rerunFailure": "fail", "rerunError": "error"}
# Strip the parts of a failure message that change run to run so the same root cause hashes the same.
_NOISE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\d+(\.\d+)?|'[^']*'|\"[^\"]*\"", re.IGNORECASE)


@dataclass
class TestResult:
    test_id: str
    file: str | None
    outcome: str  # pass | fail | error | skip
    duration_ms: int | None
    retry: int = 0
    failure_sig: str | None = None
    message: str | None = None


def failure_signature(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    first_line = text.strip().splitlines()[0]
    normalized = _NOISE.sub("#", first_line).strip().lower()
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


def _ms(seconds: str | None) -> int | None:
    try:
        return int(round(float(seconds) * 1000)) if seconds not in (None, "") else None
    except ValueError:
        return None


def _failure_text(el: ET.Element) -> str | None:
    text = el.get("message") or (el.text or "").strip()
    return text[:MESSAGE_LIMIT] if text else None


def _attempt(test_id: str, file: str | None, outcome: str, dur: int | None, el: ET.Element | None) -> TestResult:
    msg = _failure_text(el) if el is not None else None
    sig = failure_signature(msg) if outcome in ("fail", "error") else None
    return TestResult(test_id, file, outcome, dur, failure_sig=sig, message=msg if sig else None)


def parse_junit(path: str | Path) -> list[TestResult]:
    """Return one TestResult per attempt, retry index unassigned (set in assign_retries)."""
    root = ET.parse(path).getroot()
    out: list[TestResult] = []
    for tc in root.iter("testcase"):
        name = tc.get("name") or "<unnamed>"
        classname = tc.get("classname") or tc.get("class") or ""
        test_id = f"{classname}::{name}" if classname else name
        file = tc.get("file")
        dur = _ms(tc.get("time"))

        # Earlier failed attempts embedded as children (surefire style).
        for child in tc:
            if child.tag in _RETRY_TAGS:
                out.append(_attempt(test_id, file, _RETRY_TAGS[child.tag], _ms(child.get("time")), child))

        failure, error, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
        if failure is not None:
            out.append(_attempt(test_id, file, "fail", dur, failure))
        elif error is not None:
            out.append(_attempt(test_id, file, "error", dur, error))
        elif skipped is not None:
            out.append(_attempt(test_id, file, "skip", dur, None))
        else:
            out.append(_attempt(test_id, file, "pass", dur, None))
    return out


def assign_retries(results: list[TestResult]) -> list[TestResult]:
    """Number repeated attempts of the same test 0, 1, 2... in report order."""
    seen: dict[str, int] = defaultdict(int)
    for r in results:
        r.retry = seen[r.test_id]
        seen[r.test_id] += 1
    return results


def record_run(
    conn: sqlite3.Connection,
    results: list[TestResult],
    commit_sha: str,
    branch: str | None = None,
    attempt: int | None = None,
    source: str | None = None,
    external_id: str | None = None,
    started_at: datetime | None = None,
) -> tuple[int, bool]:
    """Insert a run and its results. Returns (run_id, created). Same external_id twice is a no-op."""
    if external_id:
        row = conn.execute("SELECT run_id FROM runs WHERE external_id = ?", (external_id,)).fetchone()
        if row:
            return row["run_id"], False

    if attempt is None:
        attempt = 1 + conn.execute("SELECT COUNT(*) FROM runs WHERE commit_sha = ?", (commit_sha,)).fetchone()[0]

    started = started_at.astimezone(timezone.utc) if started_at else utcnow()
    total_ms = sum(r.duration_ms or 0 for r in results) or None
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (external_id, commit_sha, branch, attempt, source, started_at, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (external_id, commit_sha, branch, attempt, source, iso(started), total_ms),
        )
        run_id = cur.lastrowid
        conn.executemany(
            "INSERT OR REPLACE INTO results (run_id, test_id, file, outcome, duration_ms, retry, failure_sig, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, r.test_id, r.file, r.outcome, r.duration_ms, r.retry, r.failure_sig, r.message) for r in results],
        )
    return run_id, True


def ingest_files(conn: sqlite3.Connection, paths: list[str | Path], **run_fields) -> tuple[int, bool, int]:
    """Parse every report file as one run. Returns (run_id, created, result_count)."""
    results: list[TestResult] = []
    for p in paths:
        results.extend(parse_junit(p))
    if not results:
        raise ValueError(f"No <testcase> elements found in {len(paths)} file(s). Is the reporter set to JUnit XML?")
    assign_retries(results)
    run_id, created = record_run(conn, results, **run_fields)
    return run_id, created, len(results)
