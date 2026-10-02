"""SQLite connection helpers. The DB path comes from --db, then FLAKEWATCH_DB, then ~/.flakewatch."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def default_db_path() -> str:
    return os.environ.get("FLAKEWATCH_DB") or str(Path.home() / ".flakewatch" / "flakewatch.db")


def connect(path: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the DB. Read-only connections use a URI so ad-hoc SQL can never write."""
    db = Path(path or default_db_path())
    if readonly:
        if not db.exists():
            raise FileNotFoundError(f"No flakewatch DB at {db}. Run `flakewatch ingest` first or set FLAKEWATCH_DB.")
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=10)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def since(days: int) -> str:
    return iso(utcnow() - timedelta(days=days))


def day_range(days: int) -> list[str]:
    """The last `days` UTC dates as YYYY-MM-DD, oldest first, ending today."""
    end = utcnow().date()
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
