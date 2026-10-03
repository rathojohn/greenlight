"""Dashboards: panels of read-only SQL, kept in greenlight's database so everyone who opens one sees the same thing.
An agent builds them through MCP (greenlight_schema for the tables, greenlight_query to try a query,
greenlight_dashboard_save to keep it); the page edits them too. The model follows Grafana and Datadog:

- panels: timeseries (lines, area or stacked bars), stat (one number, its trend and its change against the
  period before), toplist (ranked bars), table, text (a note), and row (a heading that groups the panels under it);
- variables: dropdowns at the top whose values bind to every panel's SQL as :name (NULL for All);
- the time range binds as :start and :end; compare runs a panel again over the period before; deploys and merges
  are drawn on time series.

How rows become a panel is in CONVENTIONS, which greenlight_schema hands to an agent.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from typing import Any

from . import analysis
from .db import SCHEMA, iso, parse_time, utcnow

TYPES = ("timeseries", "stat", "toplist", "table", "text", "row")
DISPLAYS = ("bars", "line", "area")
UNITS = ("auto", "count", "tokens", "ms", "percent")
CALCS = ("total", "last", "mean", "max")
WIDTHS = (3, 4, 6, 8, 12)
RESERVED = {"start", "end", "days", "today"}
MAX_PANELS = 40
MAX_VARIABLES = 6
ROWS = 1000  # per panel

CONVENTIONS = [
    "One SELECT (or WITH ... SELECT) per panel, run read-only, stopped after 5 seconds, at most 1,000 rows.",
    "Time range: :start and :end (ISO 8601 UTC), :days, :today (YYYY-MM-DD). Filter with x >= :start AND x < :end: "
    "comparing with the period before runs the same SQL with both moved back.",
    "bucket(time) groups a timestamp by hour when the range is two days or less, else by day. Times are ISO 8601 UTC "
    "text (2026-10-02T14:03:00+00:00), so substr(x, 1, 10) is the day.",
    "timeseries: the first column is the time (from bucket()), the rest are series; or three columns (time, group, "
    "value), one series per group, the six biggest and the rest as Other. display is bars (stacked), line or area.",
    "stat: one row, or (time, value) rows for a sparkline, folded by calc (total, last, mean, max). compare (on by "
    "default) shows the change from the period before.",
    "toplist: (label, value) rows in the order to show them. table: any rows; a column named test_id, session_id, "
    "pr, sha, run_id or pipeline_id opens that thing's panel. text: markdown in `text`, no SQL. row: a heading.",
    "Variables: {name, label, sql} (the first column gives the choices, an optional second their labels) or "
    "{name, label, values}. Each binds as :name, NULL when All is picked: WHERE (:branch IS NULL OR branch = :branch).",
    "unit: auto reads the value column's name (ending _ms a duration, _rate or _share a 0 to 1 percent, containing "
    "tokens or cost in k and M), or set count, tokens, ms, percent. thresholds: {warn, bad, higher_is: worse|better}.",
    "cost(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cache_write_1h_tokens) prices tokens "
    "as input tokens (output 5x, cache read 0.1x, cache write 1.25x, 2x for the hour-long cache).",
    "A test run's commit is runs.git_commit when set, else runs.commit_sha (which may end in +<hash> or @<name>). "
    "agent_sessions.title names a conversation (null when titles are off); runs.session holds its claude.ai or "
    "Claude Code id.",
]

_COST = "cost(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.cache_write_1h_tokens)"
_CONV = "COALESCE(s.title, s.remote_session, substr(u.session_id, 1, 8))"
EXAMPLES = [
    {"title": "Cost by conversation", "type": "timeseries", "display": "bars", "sql":
        f"SELECT bucket(u.minute) AS time, {_CONV} AS conversation, ROUND(SUM({_COST})) AS cost FROM agent_usage u "
        f"LEFT JOIN agent_sessions s USING (session_id) WHERE u.minute >= :start AND u.minute < :end "
        f"GROUP BY time, conversation ORDER BY time"},
    {"title": "Test failures", "type": "stat", "thresholds": {"warn": 1, "bad": 10, "higher_is": "worse"}, "sql":
        "SELECT bucket(r.started_at) AS time, COUNT(*) AS failures FROM results x JOIN runs r USING (run_id) "
        "WHERE r.started_at >= :start AND r.started_at < :end AND x.outcome IN ('fail', 'error') GROUP BY time"},
    {"title": "Failing tests", "type": "toplist", "sql":
        "SELECT x.test_id, COUNT(*) AS failures FROM results x JOIN runs r USING (run_id) WHERE r.started_at >= :start "
        "AND r.started_at < :end AND x.outcome IN ('fail', 'error') GROUP BY x.test_id ORDER BY failures DESC LIMIT 10"},
]


def params(days: int, end: Any = None) -> dict[str, Any]:
    end = end or utcnow()
    return {"start": iso(end - timedelta(days=days)), "end": iso(end), "days": days, "today": end.date().isoformat()}


def bucket_fn(days: int):
    """bucket(time): the hour for a range of two days or less, else the day."""
    def bucket(ts: Any) -> str | None:
        t = str(ts or "")
        if len(t) < 10:
            return None
        return f"{t[:10]}T{t[11:13] or '00'}:00" if days <= 2 and len(t) >= 13 else t[:10]
    return bucket


def run(conn: sqlite3.Connection, sql: str, days: int = 30, limit: int = ROWS, values: dict[str, Any] | None = None,
        end: Any = None) -> dict[str, Any]:
    return analysis.run_query(conn, sql, limit, params(days, end) | (values or {}), functions={"bucket": bucket_fn(days)})


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")[:60] or "dashboard"


def _num(v: Any, what: str) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{what} is a number")
    return v


def _panel(p: Any, i: int) -> dict[str, Any]:
    where = f"panel {i + 1}"
    if not isinstance(p, dict):
        raise ValueError(f"{where}: send an object with title, type and sql")
    kind = p.get("type") or "timeseries"
    if kind not in TYPES:
        raise ValueError(f"{where}: type is one of {', '.join(TYPES)}")
    out: dict[str, Any] = {"type": kind, "title": str(p.get("title") or "").strip()[:120]}
    where = f"panel {i + 1} ({out['title'] or kind})"
    if kind != "text" and not out["title"]:
        raise ValueError(f"{where}: give it a title")
    if kind == "row":
        return out | {"collapsed": bool(p.get("collapsed"))}
    width = p.get("width") or (3 if kind == "stat" else 6)
    if width not in WIDTHS:
        raise ValueError(f"{where}: width is one of {', '.join(map(str, WIDTHS))} (of 12 columns)")
    out |= {"width": width, "note": str(p.get("note") or "").strip()[:400]}
    if kind == "text":
        text = str(p.get("text") or "").strip()
        if not text:
            raise ValueError(f"{where}: a text panel needs its text")
        return out | {"text": text[:4000]}
    sql = str(p.get("sql") or "").strip()
    if not sql:
        raise ValueError(f"{where}: it needs its SQL")
    out["sql"] = sql[:20_000]
    unit = p.get("unit") or "auto"
    if unit not in UNITS:
        raise ValueError(f"{where}: unit is one of {', '.join(UNITS)}")
    out["unit"] = unit
    if kind == "timeseries":
        display = p.get("display") or "bars"
        if display not in DISPLAYS:
            raise ValueError(f"{where}: display is one of {', '.join(DISPLAYS)}")
        out |= {"display": display, "events": p.get("events") is not False, "compare": bool(p.get("compare"))}
    if kind == "stat":
        calc = p.get("calc") or "total"
        if calc not in CALCS:
            raise ValueError(f"{where}: calc is one of {', '.join(CALCS)}")
        out |= {"calc": calc, "compare": p.get("compare") is not False}
    t = p.get("thresholds")
    if t:
        if not isinstance(t, dict) or t.get("higher_is", "worse") not in ("worse", "better"):
            raise ValueError(f"{where}: thresholds is {{warn, bad, higher_is: worse|better}}")
        out["thresholds"] = {"warn": _num(t.get("warn"), f"{where}: warn"), "bad": _num(t.get("bad"), f"{where}: bad"),
                             "higher_is": t.get("higher_is", "worse")}
    return out


def _variable(v: Any, i: int) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError(f"variable {i + 1}: send {{name, label, sql}} or {{name, label, values}}")
    name = str(v.get("name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", name) or name in RESERVED:
        raise ValueError(f"variable {i + 1}: name it in lower case letters, digits and _, not start, end, days or today")
    out = {"name": name, "label": str(v.get("label") or name.replace("_", " ").capitalize())[:40],
           "all": v.get("all") is not False, "default": None if v.get("default") in (None, "") else str(v["default"])}
    if v.get("sql"):
        return out | {"sql": str(v["sql"]).strip()[:4000]}
    values = v.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError(f"variable {name}: give it sql, or a list of values")
    return out | {"values": [str(x) for x in values][:200]}


def _spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("send {title, panels, variables}")
    panels, variables = spec.get("panels") or [], spec.get("variables") or []
    if not isinstance(panels, list) or len(panels) > MAX_PANELS:
        raise ValueError(f"panels is a list of at most {MAX_PANELS}")
    if not isinstance(variables, list) or len(variables) > MAX_VARIABLES:
        raise ValueError(f"variables is a list of at most {MAX_VARIABLES}")
    vs = [_variable(v, i) for i, v in enumerate(variables)]
    if len({v["name"] for v in vs}) < len(vs):
        raise ValueError("two variables have the same name")
    return {"description": str(spec.get("description") or "").strip()[:400], "variables": vs,
            "panels": [_panel(p, i) for i, p in enumerate(panels)]}


def options(conn: sqlite3.Connection, v: dict[str, Any], days: int) -> list[list[str]]:
    """A variable's choices: [value, label]."""
    if "values" in v:
        return [[x, x] for x in v["values"]]
    rows = run(conn, v["sql"], days, limit=500)["rows"]
    return [[str(r[0]), str(r[1] if len(r) > 1 and r[1] is not None else r[0])] for r in rows if r and r[0] is not None]


def save(conn: sqlite3.Connection, title: str, spec: dict[str, Any], dashboard_id: str | None = None,
         check: sqlite3.Connection | None = None, days: int = 30) -> dict[str, Any]:
    """Create or replace a dashboard. Each variable's and panel's SQL runs first (with every variable on All), and
    nothing is saved while one fails, so a saved dashboard always draws."""
    title = str(title or "").strip()[:120]
    if not title:
        raise ValueError("a dashboard needs a title")
    clean = _spec(spec)
    q = check or conn
    failed, tried = [], []
    for v in clean["variables"]:
        try:
            options(q, v, days)
        except (ValueError, sqlite3.Error) as e:
            failed.append(f"variable {v['name']}: {e}")
    values = {v["name"]: None for v in clean["variables"]}
    for i, p in enumerate(clean["panels"]):
        if "sql" not in p:
            continue
        try:
            out = run(q, p["sql"], days, values=values)
            tried.append({"panel": i + 1, "title": p["title"], "columns": out["columns"], "rows": len(out["rows"]),
                          "first_rows": out["rows"][:3]})
        except (ValueError, sqlite3.Error) as e:
            failed.append(f"panel {i + 1} ({p['title']}): {e}")
    if failed:
        raise ValueError("Nothing saved. " + " ".join(failed))
    did = slug(dashboard_id or title)
    with conn:
        conn.execute("INSERT INTO dashboards (dashboard_id, title, spec, updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(dashboard_id) DO UPDATE SET title = excluded.title, spec = excluded.spec, "
                     "updated_at = excluded.updated_at", (did, title, json.dumps(clean), iso(utcnow())))
    return {"id": did, "title": title, "route": f"#/dashboards/{did}", "panels": tried}


def delete(conn: sqlite3.Connection, dashboard_id: str) -> bool:
    with conn:
        return conn.execute("DELETE FROM dashboards WHERE dashboard_id = ?", (dashboard_id,)).rowcount > 0


def all_dashboards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for did, title, spec, at in conn.execute("SELECT dashboard_id, title, spec, updated_at FROM dashboards ORDER BY title"):
        s = json.loads(spec or "{}")
        out.append({"id": did, "title": title, "description": s.get("description") or "",
                    "panels": sum(p.get("type") != "row" for p in s.get("panels", [])), "updated_at": at})
    return out


def get(conn: sqlite3.Connection, dashboard_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT dashboard_id, title, spec, updated_at FROM dashboards WHERE dashboard_id = ?",
                     (dashboard_id,)).fetchone()
    if not r:
        raise LookupError(f"No dashboard called {dashboard_id}")
    return {"id": r[0], "title": r[1], "updated_at": r[3], **json.loads(r[2] or "{}")}


def _shift(result: dict[str, Any], days: int) -> dict[str, Any]:
    """The period before's rows with their times moved forward a period, so they line up with this one's."""
    def move(x: Any) -> Any:
        t = parse_time(x) if isinstance(x, str) and re.match(r"\d{4}-\d{2}-\d{2}", x) else None
        if t is None:
            return x
        moved = (t + timedelta(days=days)).isoformat()
        return moved[:len(x)] if len(x) <= 16 else iso(t + timedelta(days=days))
    return result | {"rows": [[move(r[0]), *r[1:]] for r in result["rows"]]}


def events(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """Deploys and merges in the range, for the markers on time series."""
    out = [{"at": r[0], "kind": "deploy", "label": f"Deployed {r[2] or ''} to {r[1]}".replace("  ", " ")}
           for r in conn.execute("SELECT deployed_at, environment, version FROM deployments WHERE deployed_at >= ? "
                                 "AND deployed_at < ? ORDER BY deployed_at", (start, end))]
    out += [{"at": r[0], "kind": "merge", "label": f"Merged #{r[1]} {r[2] or ''}".strip()}
            for r in conn.execute("SELECT merged_at, number, title FROM pull_requests WHERE merged_at >= ? AND "
                                  "merged_at < ? ORDER BY merged_at", (start, end))]
    return sorted(out, key=lambda e: e["at"])


def render(conn: sqlite3.Connection, dashboard_id: str, days: int = 30, chosen: dict[str, str] | None = None) -> dict[str, Any]:
    """A dashboard with its variables' choices and each panel's rows (or the error its SQL gave, since data can change
    under a saved query), the period before's rows for panels that compare, and the deploys and merges."""
    d = get(conn, dashboard_id)
    chosen = chosen or {}
    values = {}
    for v in d["variables"]:
        try:
            v["options"] = options(conn, v, days)
        except (ValueError, sqlite3.Error) as e:
            v["options"], v["error"] = [], str(e)
        pick = chosen.get(v["name"], v["default"])
        if pick in (None, "", "__all") and v["all"]:
            pick = None
        elif pick in (None, "", "__all"):
            pick = v["options"][0][0] if v["options"] else None
        v["value"] = values[v["name"]] = pick
    end = utcnow()
    before = end - timedelta(days=days)
    for p in d["panels"]:
        if "sql" not in p:
            continue
        try:
            p["result"] = run(conn, p["sql"], days, values=values, end=end)
            if p.get("compare"):
                p["previous"] = _shift(run(conn, p["sql"], days, values=values, end=before), days)
        except (ValueError, sqlite3.Error) as e:
            p["error"] = str(e)
    return d | {"window_days": days, "start": iso(before), "end": iso(end), "events": events(conn, iso(before), iso(end))}


def schema() -> dict[str, Any]:
    """Every table and column, with what schema.sql says about them, and how a panel's rows are drawn: what an agent
    needs to write one."""
    tables, about = [], []
    current = None
    for line in SCHEMA.splitlines():
        s = line.strip()
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", s)
        if m:
            current = {"table": m.group(1), "about": " ".join(about), "columns": []}
            tables.append(current)
            about = []
        elif s.startswith("--"):
            about.append(s.lstrip("- ").strip())
        elif s.startswith(")"):
            current = None
        elif current is not None and s and not s.upper().startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN")):
            code, _, note = s.partition("--")
            parts = code.strip().rstrip(",").split()
            if len(parts) >= 2:  # "name TYPE: what it is", compact for an agent's context
                current["columns"].append(f"{parts[0]} {parts[1].rstrip(',')}" + (f": {note.strip()}" if note.strip() else ""))
        elif not s:
            about = []
    return {"tables": tables, "conventions": CONVENTIONS, "examples": EXAMPLES, "types": list(TYPES),
            "displays": list(DISPLAYS), "units": list(UNITS), "calcs": list(CALCS), "widths": list(WIDTHS)}


_BRANCH = "(:branch IS NULL OR {col} = :branch)"
STARTER = {"title": "What's trending", "description": "What changed in this period against the one before: "
                                                      "where the tokens went, what failed, what shipped.",
           "variables": [{"name": "branch", "label": "Branch", "sql":
                          "SELECT branch FROM agent_usage WHERE minute >= :start AND branch != '' UNION "
                          "SELECT branch FROM runs WHERE started_at >= :start AND branch IS NOT NULL ORDER BY 1"}],
           "panels": [
    {"type": "stat", "title": "Cost", "unit": "tokens", "note": "Tokens priced as input tokens.",
     "thresholds": {"higher_is": "worse"},
     "sql": f"SELECT bucket(u.minute) AS time, ROUND(SUM({_COST})) AS cost FROM agent_usage u WHERE u.minute >= :start "
            f"AND u.minute < :end AND {_BRANCH.format(col='u.branch')} GROUP BY time"},
    {"type": "stat", "title": "Test failures", "thresholds": {"warn": 1, "bad": 10, "higher_is": "worse"},
     "note": "Failed or errored results, every attempt counted.",
     "sql": "SELECT bucket(r.started_at) AS time, COUNT(*) AS failures FROM results x JOIN runs r USING (run_id) "
            f"WHERE r.started_at >= :start AND r.started_at < :end AND {_BRANCH.format(col='r.branch')} "
            "AND x.outcome IN ('fail', 'error') GROUP BY time"},
    {"type": "stat", "title": "CI failure rate", "calc": "mean", "unit": "percent",
     "thresholds": {"warn": 0.1, "bad": 0.25, "higher_is": "worse"}, "note": "Share of GitHub Actions runs that failed.",
     "sql": "SELECT bucket(created_at) AS time, AVG(status IN ('failure', 'timed_out', 'startup_failure')) AS fail_rate "
            f"FROM pipelines WHERE created_at >= :start AND created_at < :end AND {_BRANCH.format(col='branch')} "
            "GROUP BY time"},
    {"type": "stat", "title": "Commits from conversations", "thresholds": {"higher_is": "better"},
     "note": "Commits a recorded Claude Code session made.",
     "sql": "SELECT bucket(at) AS time, COUNT(*) AS commits FROM agent_commits WHERE kind = 'commit' AND at >= :start "
            f"AND at < :end AND {_BRANCH.format(col='branch')} GROUP BY time"},
    {"type": "row", "title": "Where the tokens went"},
    {"type": "timeseries", "title": "Cost by conversation", "display": "bars", "width": 8, "compare": True,
     "note": "Split by the Claude Code session that spent it. The dashed line is the period before.",
     "sql": f"SELECT bucket(u.minute) AS time, {_CONV} AS conversation, ROUND(SUM({_COST})) AS cost FROM agent_usage u "
            f"LEFT JOIN agent_sessions s USING (session_id) WHERE u.minute >= :start AND u.minute < :end AND "
            f"{_BRANCH.format(col='u.branch')} GROUP BY time, conversation ORDER BY time"},
    {"type": "toplist", "title": "What rode along the most", "width": 4, "unit": "tokens",
     "note": "Each thing that entered a context, by the cache reads of it after.",
     "sql": "SELECT label AS item, SUM(carried_tokens) AS re_read_tokens FROM agent_context_items WHERE seg_start >= "
            f":start AND seg_start < :end AND {_BRANCH.format(col='branch')} GROUP BY label, kind "
            "ORDER BY re_read_tokens DESC LIMIT 10"},
    {"type": "timeseries", "title": "Re-read tokens by what put them in context", "display": "area", "width": 12,
     "note": "Cache reads of what tool results added, until a compaction.",
     "sql": "SELECT bucket(minute) AS time, category, SUM(carried_tokens) AS tokens FROM agent_context WHERE minute >= "
            f":start AND minute < :end AND {_BRANCH.format(col='branch')} AND category NOT IN ('system', 'conversation') "
            "GROUP BY time, category ORDER BY time"},
    {"type": "row", "title": "Tests and CI"},
    {"type": "timeseries", "title": "Failed tests", "display": "bars", "compare": True,
     "sql": "SELECT bucket(r.started_at) AS time, x.test_id AS test, COUNT(*) AS failures FROM results x JOIN runs r "
            f"USING (run_id) WHERE r.started_at >= :start AND r.started_at < :end AND {_BRANCH.format(col='r.branch')} "
            "AND x.outcome IN ('fail', 'error') GROUP BY time, test ORDER BY time"},
    {"type": "timeseries", "title": "Failed CI runs by workflow", "display": "bars",
     "sql": "SELECT bucket(created_at) AS time, workflow, COUNT(*) AS failed FROM pipelines WHERE created_at >= :start "
            f"AND created_at < :end AND {_BRANCH.format(col='branch')} AND status IN ('failure', 'timed_out', "
            "'startup_failure') GROUP BY time, workflow ORDER BY time"},
    {"type": "table", "title": "Tests that failed most", "width": 8,
     "sql": "SELECT x.test_id, COUNT(DISTINCT r.run_id) AS runs, COUNT(DISTINCT CASE WHEN x.outcome IN ('fail', 'error') "
            "THEN r.run_id END) AS failed_runs, ROUND(1.0 * COUNT(DISTINCT CASE WHEN x.outcome IN ('fail', 'error') THEN "
            "r.run_id END) / COUNT(DISTINCT r.run_id), 3) AS fail_rate, MAX(r.started_at) AS last_run FROM results x "
            f"JOIN runs r USING (run_id) WHERE r.started_at >= :start AND r.started_at < :end AND "
            f"{_BRANCH.format(col='r.branch')} GROUP BY x.test_id HAVING failed_runs > 0 ORDER BY failed_runs DESC LIMIT 15"},
    {"type": "timeseries", "title": "Slowest tests", "display": "line", "width": 4,
     "note": "The five slowest passing tests, their average duration.",
     "sql": "WITH slow AS (SELECT x.test_id FROM results x JOIN runs r USING (run_id) WHERE r.started_at >= :start AND "
            "r.started_at < :end AND x.outcome = 'pass' AND x.duration_ms IS NOT NULL GROUP BY x.test_id ORDER BY "
            "AVG(x.duration_ms) DESC LIMIT 5) SELECT bucket(r.started_at) AS time, x.test_id AS test, "
            "ROUND(AVG(x.duration_ms)) AS duration_ms FROM results x JOIN runs r USING (run_id) JOIN slow USING (test_id) "
            "WHERE r.started_at >= :start AND r.started_at < :end AND x.outcome = 'pass' GROUP BY time, test ORDER BY time"},
]}


def add_starter(conn: sqlite3.Connection) -> dict[str, Any]:
    return save(conn, STARTER["title"], STARTER)
