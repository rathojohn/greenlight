"""SQLite connection helpers. The DB path comes from --db, then GREENLIGHT_DB, then the config file,
then ~/.greenlight/greenlight.db."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
SCHEMA_VERSION = 3

# Columns added after their table first shipped: (table, column, declaration). schema.sql has them
# for new databases; these bring older ones up to date.
MIGRATIONS = [
    ("runs", "git_commit", "TEXT"),
    ("runs", "total_tests", "INTEGER"),
    ("runs", "session", "TEXT"),
    ("runs", "command", "TEXT"),
    ("runs", "url", "TEXT"),
    ("results", "flags", "TEXT"),
    ("steps", "started_at", "TEXT"),
]

_config_db: str | None = None


def set_config_db(path: str | None) -> None:
    """The db path from greenlight.toml, used when neither --db nor GREENLIGHT_DB is set."""
    global _config_db
    _config_db = path


def default_db_path() -> str:
    home = Path(os.path.expanduser(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight"))
    return os.environ.get("GREENLIGHT_DB") or _config_db or str(home / "greenlight.db")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    for table, column, decl in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def connect(path: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the DB. Read-only connections use a URI so ad-hoc SQL can never write. An older
    database is upgraded first, so read-only callers can rely on the current columns."""
    db = Path(os.path.expanduser(path or default_db_path()))
    if readonly:
        if not db.exists():
            raise FileNotFoundError(f"No greenlight DB at {db}. Run `greenlight ingest` or `greenlight sync` first, or set GREENLIGHT_DB.")
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.close()
            upgrade = sqlite3.connect(db, timeout=10)
            try:
                migrate(upgrade)
            finally:
                upgrade.close()
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=10)
        migrate(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    """ISO 8601 from GitHub, git or JS ("2026-10-02T01:14:34.844Z") as an aware datetime."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def norm_time(value: str | None) -> str | None:
    """Any ISO 8601 timestamp as greenlight stores it: UTC, seconds, +00:00."""
    dt = parse_time(value)
    return iso(dt) if dt else None


def since(days: int) -> str:
    return iso(utcnow() - timedelta(days=days))


def day_range(days: int) -> list[str]:
    """The last `days` UTC dates as YYYY-MM-DD, oldest first, ending today."""
    end = utcnow().date()
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
