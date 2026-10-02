"""Keep one GitHub issue per flaky test and per perf regression, the way Datadog's flaky test
management keeps a case per test.

`flakewatch issues` plans; `--apply` does it:
  create   a flaky test (flipped on min_flips+ commits) or a slower-than-base check with no issue yet
  update   an open issue whose body is out of date (the body is regenerated; comment below it instead)
  reopen   a closed issue whose test flipped, or ran slower, again after it was closed
  healed   one comment on an open flaky issue once its test has run clean long enough to close it
  link     an existing issue that looks like the same check: mark it instead of opening a duplicate
Each managed issue carries a hidden marker, <!-- flakewatch:flaky:<test id> -->, which is how sync
and the next plan find it. Nothing is ever closed for you.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from . import analysis
from .db import iso, since, utcnow
from .ghsync import MARKER, upsert_issue
from .github import GitHub, GitHubError

HEALED = "<!-- flakewatch:healed -->"
LABEL_COLORS = {"flaky-test": "B07A00", "perf-regression": "6366A8", "quarantined": "55606C"}
_WORDS = re.compile(r"[a-z0-9]+")


@dataclass
class Action:
    kind: str                       # create, update, reopen, healed, link
    key: str                        # flaky:<test id> / perf:<test id>
    title: str
    number: int | None = None
    body: str | None = None
    labels: list[str] = field(default_factory=list)
    comment: str | None = None
    why: str = ""


def marker(key: str) -> str:
    return f"<!-- flakewatch:{key} -->"


def _title(prefix: str, test_id: str) -> str:
    t = f"{prefix}: {test_id.replace('::', ': ', 1)}"
    return t if len(t) <= 240 else t[:237] + "..."


def _fence(text: str | None) -> str:
    body = (text or "").replace("```", "'''").strip()
    return f"```\n{body}\n```" if body else "_no message_"


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _short(sha: str | None) -> str:
    if not sha:
        return ""
    base, plus, _ = sha.partition("+")
    return base[:8] + (" + local edits" if plus else "")


def _run_link(r: dict[str, Any]) -> str:
    return f"[run {r['run_id']}]({r['url']})" if r.get("url") else f"run {r['run_id']}"


def _rerun_hint(test_id: str, source: str | None) -> str:
    if source and source.startswith("playtest"):
        return f"`node tools/playtest/run.cjs {test_id.split('::', 1)[0]} --rerun`"
    return "just this test"


# ---------- bodies ----------
def flaky_body(conn: sqlite3.Connection, s: dict[str, Any], window: int, quarantine_label: str) -> str:
    test_id = s["test_id"]
    hist = conn.execute(
        """SELECT ru.run_id, ru.started_at, ru.commit_sha, ru.url, ru.source, r.outcome, r.retry, r.flags
           FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? ORDER BY ru.started_at DESC, r.retry DESC LIMIT 12""", (test_id,)).fetchall()
    sigs = conn.execute(
        """SELECT r.message, COUNT(*) AS n, MAX(ru.started_at) AS last FROM results r JOIN runs ru ON ru.run_id = r.run_id
           WHERE r.test_id = ? AND r.failure_sig IS NOT NULL AND ru.started_at >= ?
           GROUP BY r.failure_sig ORDER BY n DESC LIMIT 3""", (test_id, since(window))).fetchall()
    source = hist[0]["source"] if hist else None
    lines = [
        marker(f"flaky:{test_id}"),
        f"`{test_id}` both failed and passed on the same code on **{s['flip_shas']} of {s['eligible_shas']}** "
        f"commits it ran on more than once in the last {window} days.",
        "",
        "| | |", "| --- | --- |",
        f"| Executions | {s['executions']} |",
        f"| Failures | {s['failures']} |",
        f"| Flake score | {s['flake_score']:.2f} (lower bound of the flip rate) |",
        f"| Last flip | {(s.get('last_flip_at') or 'n/a')[:16].replace('T', ' ')} UTC |",
        f"| Quarantined | {'yes' if s.get('quarantined') else 'no'} |",
        "",
        "### Recent results",
        "| When (UTC) | Code | Result | Run |", "| --- | --- | --- | --- |",
    ]
    for r in hist:
        result = r["outcome"] + (f" (retry {r['retry']})" if r["retry"] else "") + (" *" if r["flags"] == "inferred" else "")
        lines.append(f"| {r['started_at'][:16].replace('T', ' ')} | `{_short(r['commit_sha'])}` | {result} | {_run_link(dict(r))} |")
    if any(r["flags"] == "inferred" for r in hist):
        lines.append("\n\\* inferred: the run's record names only failures, and this test's suite passed.")
    if sigs:
        lines += ["", "### Failure messages"]
        for g in sigs:
            lines += [f"{g['n']}x, last {g['last'][:16].replace('T', ' ')} UTC", _fence(g["message"])]
    lines += [
        "", "### What to do",
        f"- The gate treats this test as flaky: when it fails, rerun {_rerun_hint(test_id, source)}, not the suite.",
        f"- Add the `{quarantine_label}` label to stop it blocking while it gets fixed. Remove the label or close "
        "this issue to release it.",
        "- Close this issue when the fix merges. flakewatch reopens it if the test flips again.",
        "", f"<sub>Kept up to date by flakewatch, last {iso(utcnow())[:16].replace('T', ' ')} UTC. The text above "
            "is regenerated on each run, so comment below instead of editing it.</sub>",
    ]
    return "\n".join(lines)


def perf_rows(conn: sqlite3.Connection, window: int) -> dict[str, list[dict[str, Any]]]:
    """Runs where a check ran slower than its base build, newest first, with its numbers when known."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
            """SELECT r.test_id, ru.run_id, ru.started_at, ru.commit_sha, ru.url, ru.session, r.message,
                      (SELECT value FROM metrics m WHERE m.run_id = r.run_id AND m.test_id = r.test_id AND m.name = 'value_ms') AS value_ms,
                      (SELECT value FROM metrics m WHERE m.run_id = r.run_id AND m.test_id = r.test_id AND m.name = 'base_ms') AS base_ms
               FROM results r JOIN runs ru ON ru.run_id = r.run_id
               WHERE ',' || COALESCE(r.flags, '') || ',' LIKE '%,slower,%' AND ru.started_at >= ?
               ORDER BY ru.started_at DESC""", (since(window),)):
        out.setdefault(r["test_id"], []).append(dict(r))
    return out


def perf_body(test_id: str, rows: list[dict[str, Any]]) -> str:
    def ms(v: float | None) -> str:
        return f"{v:g} ms" if v is not None else "n/a"
    lines = [
        marker(f"perf:{test_id}"),
        f"`{test_id}` ran slower than the build it was measured beside in **{len(rows)}** run(s) since "
        f"{rows[-1]['started_at'][:16].replace('T', ' ')} UTC.",
        "", "| When (UTC) | Code | This build | Base build | Run |", "| --- | --- | --- | --- | --- |",
    ]
    for r in rows[:15]:
        lines.append(f"| {r['started_at'][:16].replace('T', ' ')} | `{_short(r['commit_sha'])}` | {ms(r['value_ms'])} | "
                     f"{ms(r['base_ms'])} | {_run_link(r)} |")
    detail = next((r["message"] for r in rows if r.get("message")), None)
    if detail:
        lines += ["", "Latest detail:", _fence(detail)]
    lines += [
        "", "A slower check does not fail the gate; it gets this issue instead. Close it when the fix merges, or with "
            "the numbers if a rerun shows it was noise (within 10% of the base). flakewatch reopens it if it runs "
            "slower again.",
        "", f"<sub>Kept up to date by flakewatch, last {iso(utcnow())[:16].replace('T', ' ')} UTC. Comment below "
            "instead of editing the text above.</sub>",
    ]
    return "\n".join(lines)


# ---------- planning ----------
def _words(text: str) -> set[str]:
    return {w for w in _WORDS.findall(text.lower()) if len(w) >= 3}


def lookalike(conn: sqlite3.Connection, test_id: str) -> dict[str, Any] | None:
    """An open issue flakewatch doesn't manage whose title holds most of the check's words, e.g. a perf
    issue someone opened by hand before flakewatch ran."""
    name = _words(test_id.split("::", 1)[-1])
    if len(name) < 3:
        return None
    best = None
    for r in conn.execute("SELECT number, title FROM issues WHERE state = 'open' AND fw_key IS NULL"):
        share = len(name & _words(r["title"] or "")) / len(name)
        if share >= 0.7 and (best is None or share > best[0]):
            best = (share, dict(r))
    return best[1] if best else None


def plan(conn: sqlite3.Connection, opts: dict[str, Any]) -> list[Action]:
    window = int(opts.get("window_days", 30))
    min_flips = int(opts.get("min_flips", 2))
    flaky_label, perf_label = opts.get("flaky_label", "flaky-test"), opts.get("perf_label", "perf-regression")
    qlabel = opts.get("quarantine_label", "quarantined")
    managed = {r["fw_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM issues WHERE fw_key IS NOT NULL ORDER BY state = 'open', number")}
    actions: list[Action] = []

    stats = analysis.flake_stats(conn, window, min_flips=min_flips)
    for s in sorted(stats.values(), key=lambda s: s["flake_score"], reverse=True):
        if s["flip_shas"] < min_flips:
            continue
        key = f"flaky:{s['test_id']}"
        title = _title("Flaky test", s["test_id"])
        issue = managed.get(key)
        body = flaky_body(conn, s, window, qlabel)
        if issue is None:
            twin = lookalike(conn, s["test_id"])
            if twin:
                actions.append(Action("link", key, twin["title"], twin["number"],
                                      why=f"open issue #{twin['number']} looks like this test; marking it instead of opening a new one"))
            else:
                actions.append(Action("create", key, title, body=body, labels=[flaky_label],
                                      why=f"flipped on {s['flip_shas']}/{s['eligible_shas']} commits in {window} days"))
        elif issue["state"] == "open":
            actions.append(Action("update", key, issue["title"], issue["number"], body=body, why="refresh the numbers"))
        elif s.get("last_flip_at") and issue["closed_at"] and s["last_flip_at"] > issue["closed_at"]:
            actions.append(Action("reopen", key, issue["title"], issue["number"], body=body,
                                  comment=f"Flipped again at {s['last_flip_at'][:16].replace('T', ' ')} UTC, after this was "
                                          "closed. Reopening.", why="flipped again after it was closed"))

    healed_days, healed_runs = int(opts.get("healed_days", 14)), int(opts.get("healed_runs", 10))
    recent = analysis.flake_stats(conn, healed_days)
    for key, issue in managed.items():
        if not key.startswith("flaky:") or issue["state"] != "open":
            continue
        s = recent.get(key.split(":", 1)[1])
        if s and s["executions"] >= healed_runs and s["failures"] == 0:
            actions.append(Action("healed", key, issue["title"], issue["number"],
                                  comment=f"{HEALED}\nNo failures in {s['executions']} runs over the last {healed_days} days. "
                                          "Close this if the fix is in; flakewatch reopens it if the test flips again.",
                                  why=f"clean for {s['executions']} runs"))

    for test_id, rows in sorted(perf_rows(conn, window).items()):
        key = f"perf:{test_id}"
        issue = managed.get(key)
        body = perf_body(test_id, rows)
        if issue is None:
            twin = lookalike(conn, test_id)
            if twin:
                actions.append(Action("link", key, twin["title"], twin["number"],
                                      why=f"open issue #{twin['number']} looks like this check; marking it instead of opening a new one"))
            else:
                actions.append(Action("create", key, _title("Perf regression", test_id), body=body, labels=[perf_label],
                                      why=f"slower than its base build in {len(rows)} run(s)"))
        elif issue["state"] == "open":
            actions.append(Action("update", key, issue["title"], issue["number"], body=body, why="new measurements"))
        elif issue["closed_at"] and rows[0]["started_at"] > issue["closed_at"]:
            actions.append(Action("reopen", key, issue["title"], issue["number"], body=body,
                                  comment=f"Ran slower than its base again at {rows[0]['started_at'][:16].replace('T', ' ')} "
                                          "UTC, after this was closed. Reopening.", why="slower again after it was closed"))
    return actions


# ---------- applying ----------
def _ensure_labels(gh: GitHub, labels: set[str]) -> None:
    for name in labels:
        try:
            gh.get(f"/labels/{name}")
        except GitHubError as e:
            if e.status != 404:
                raise
            gh.post("/labels", {"name": name, "color": LABEL_COLORS.get(name, "B07A00"),
                                "description": "Managed by flakewatch"})


def _has_healed_comment(gh: GitHub, number: int) -> bool:
    return any(HEALED in (c.get("body") or "") for c in gh.paginate(f"/issues/{number}/comments"))


def apply(conn: sqlite3.Connection, gh: GitHub, actions: list[Action]) -> list[dict[str, Any]]:
    _ensure_labels(gh, {lb for a in actions for lb in a.labels})
    done = []
    for a in actions:
        try:
            result = _apply_one(gh, a)
        except GitHubError as e:
            done.append({**asdict(a), "body": None, "result": f"error: {e}"})
            continue
        number = a.number
        if isinstance(result, dict) and "number" in result:
            number = result["number"]
            with conn:
                upsert_issue(conn, result)
        done.append({**asdict(a), "body": None, "number": number,
                     "result": "done" if result is not None else "unchanged"})
    return done


def _apply_one(gh: GitHub, a: Action) -> dict[str, Any] | None:
    if a.kind == "create":
        return gh.post("/issues", {"title": a.title, "body": a.body, "labels": a.labels})
    if a.kind == "update":
        current = gh.get(f"/issues/{a.number}")
        if _strip_stamp(current.get("body")) == _strip_stamp(a.body):
            return None
        return gh.patch(f"/issues/{a.number}", {"body": a.body})
    if a.kind == "reopen":
        issue = gh.patch(f"/issues/{a.number}", {"state": "open", "body": a.body})
        gh.post(f"/issues/{a.number}/comments", {"body": a.comment})
        return issue
    if a.kind == "healed":
        if _has_healed_comment(gh, a.number):
            return None
        gh.post(f"/issues/{a.number}/comments", {"body": a.comment})
        return gh.get(f"/issues/{a.number}")
    if a.kind == "link":
        return link(gh, a.number, a.key)
    raise ValueError(f"unknown action {a.kind}")


def _strip_stamp(body: str | None) -> str:
    """The body without its 'last updated' line, so a refresh with the same numbers is a no-op."""
    return re.sub(r"<sub>Kept up to date by flakewatch.*?</sub>", "", body or "", flags=re.S).strip()


def link(gh: GitHub, number: int, key: str) -> dict[str, Any]:
    """Put flakewatch's marker on an existing issue so it manages that one from now on."""
    issue = gh.get(f"/issues/{number}")
    body = issue.get("body") or ""
    if MARKER.search(body):
        return issue
    return gh.patch(f"/issues/{number}", {"body": f"{marker(key)}\n{body}"})


def summarize(actions: list[Action]) -> str:
    if not actions:
        return "Nothing to do: every flaky test and perf regression already has an up-to-date issue."
    lines = []
    for a in actions:
        target = f"#{a.number}" if a.number else "new"
        lines.append(f"{a.kind:<7} {target:<6} {a.title}  ({a.why})")
    return "\n".join(lines)


def as_json(actions: list[Action]) -> str:
    return json.dumps([{k: v for k, v in asdict(a).items() if k != "body"} for a in actions], indent=2)
