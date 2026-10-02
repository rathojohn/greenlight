"""What to do next, ranked by what's at stake, and the brief an agent starts a session with.

insights() reads what greenlight already keeps (flake stats, quarantine, the default branch's latest runs, token
usage and where context went) and returns findings, each with its evidence and one action. brief() turns the
ones an agent can act on into a few lines: what fails on the default branch (not the agent's doing), which
tests are flaky (rerun once, don't debug), what's quarantined, and the habits that cost the most in this repo.
The hosted server puts the brief in its MCP instructions; `greenlight brief --hook` gives it to subagents.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from statistics import median
from typing import Any

from . import analysis, usage
from .db import since

DEFAULT_BRANCHES = ("main", "master")
SEVERITY = {"high": 0, "medium": 1, "low": 2}
KEPT = {"read": "file reads", "test": "test output", "search": "search results", "shell": "shell output",
        "image": "images", "web": "web pages", "git": "git output", "subagent": "subagent reports", "build": "build output"}
CATEGORY_TITLES = {"read": "File reads", "test": "Test runs", "search": "Searches", "shell": "Shell outputs",
                   "image": "Images", "web": "Web pages", "git": "git outputs", "subagent": "Subagent reports",
                   "build": "Builds"}
# what Claude can do about a context category itself (the dashboard's advice is for people)
AGENT_HABITS = {
    "read": "read files with an offset and limit, or Grep for the lines first",
    "test": "run only the tests the change needs, with a quiet reporter, and send long logs to a file",
    "search": "narrow Grep and Glob with a path or glob and a result limit",
    "shell": "pipe long command output through tail or grep",
    "image": "look at screenshots in a subagent and keep only its verdict",
    "web": "ask WebFetch a narrow question",
    "git": "use git diff --stat, log --oneline -n",
    "subagent": "ask subagents for short reports",
    "build": "keep build output quiet unless it fails",
}


def _name(test_id: str) -> str:
    return test_id.rsplit("::", 1)[-1]


def red_on_default(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    """Tests whose latest result on the default branch failed, with the run that started the streak. Quarantined
    tests are left out: the gate already ignores them."""
    marks = ",".join("?" * len(DEFAULT_BRANCHES))
    rows = conn.execute(
        f"""SELECT r.test_id, ru.run_id, ru.started_at, COALESCE(ru.git_commit, ru.commit_sha) AS sha, r.outcome
            FROM results r JOIN runs ru ON ru.run_id = r.run_id
            WHERE ru.branch IN ({marks}) AND ru.started_at >= ? AND r.outcome != 'skip'
              AND r.retry = (SELECT MAX(retry) FROM results x WHERE x.run_id = r.run_id AND x.test_id = r.test_id)
              AND r.test_id NOT IN (SELECT test_id FROM quarantine)
            ORDER BY ru.started_at DESC""", (*DEFAULT_BRANCHES, since(days))).fetchall()
    by_test: dict[str, list] = defaultdict(list)
    for r in rows:
        by_test[r["test_id"]].append(r)
    out = []
    for test, runs in by_test.items():
        if runs[0]["outcome"] not in ("fail", "error"):
            continue
        streak = 0
        while streak < len(runs) and runs[streak]["outcome"] in ("fail", "error"):
            streak += 1
        first = runs[streak - 1]
        out.append({"test_id": test, "runs": streak, "since": first["started_at"], "since_sha": first["sha"],
                    "last_run": runs[0]["run_id"], "passed_before": streak < len(runs)})
    return sorted(out, key=lambda t: t["since"])


def insights(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Findings, most at stake first. Each: id, severity, title, detail, stake (cost in input tokens, when it's
    about tokens), action ({label, panel or href}), and agent: a line for an agent's brief, or None."""
    out: list[dict[str, Any]] = []
    stats = analysis.flake_stats(conn, days)
    flaky = {t: s for t, s in stats.items() if s["flip_shas"] > 0}

    for t in red_on_default(conn, days):
        n = _name(t["test_id"])
        out.append({"id": f"red:{t['test_id']}", "severity": "high", "stake": None,
                    "title": f"{n} fails on the default branch",
                    "detail": f"Failed the last {t['runs']} run{'s' if t['runs'] != 1 else ''} there, since "
                              f"{str(t['since_sha'])[:8]}. A branch that fails it didn't break it.",
                    "action": {"label": "Open", "panel": f"test:{t['test_id']}"},
                    "agent": None, "red": t, "rank": 0})

    use = usage.summary(conn, days) if conn.execute("SELECT 1 FROM agent_usage LIMIT 1").fetchone() else None
    total = (use or {}).get("totals", {}).get("weighted") or 0
    for t in (use or {}).get("by_test", []):
        s = flaky.get(t["test_id"])
        if not s or not t["weighted"]:
            continue
        n = _name(t["test_id"])
        out.append({"id": f"flaky-cost:{t['test_id']}", "severity": "high" if total and t["weighted"] >= total * .02
                    else "medium", "stake": t["weighted"],
                    "title": f"Claude spent {_tk(t['weighted'])} while flaky {n} was red",
                    "detail": f"It passed and failed on the same code on {s['flip_shas']} of {s['eligible_shas']} commits. "
                              f"{'It is quarantined now.' if s['quarantined'] else 'Quarantine it, or fix the test.'}",
                    "action": {"label": "Open", "panel": f"test:{t['test_id']}"}, "agent": None})

    sweep = analysis.sweep(conn, days)
    if sweep["to_quarantine"]:
        names = ", ".join(_name(s["test_id"]) for s in sweep["to_quarantine"][:3])
        more = len(sweep["to_quarantine"]) - 3
        out.append({"id": "sweep", "severity": "medium", "stake": None,
                    "title": f"{len(sweep['to_quarantine'])} flaky test{'s' if len(sweep['to_quarantine']) != 1 else ''} "
                             "could be quarantined",
                    "detail": f"{names}{f' and {more} more' if more > 0 else ''}: they flip often enough that each "
                              "failure costs a rerun.", "action": {"label": "Review", "href": "#/quarantine"},
                    "agent": None})
    if sweep["release_candidates"]:
        n = len(sweep["release_candidates"])
        out.append({"id": "release", "severity": "low", "stake": None,
                    "title": f"{n} quarantined test{'s' if n != 1 else ''} passed every run for 14 days",
                    "detail": "Releasing them makes their failures count again.",
                    "action": {"label": "Review", "href": "#/quarantine"}, "agent": None})

    if use and total:
        out += _token_insights(use, total)
    out.sort(key=lambda i: (SEVERITY[i["severity"]], i.get("rank", 1), -(i["stake"] or 0)))
    return out


def _tk(n: float) -> str:
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n))


def _pct(n: float, total: float) -> str:
    return f"{n / total:.0%}"


def _token_insights(use: dict[str, Any], total: int) -> list[dict[str, Any]]:
    out = []
    cx = use.get("context") or {}
    sw = cx.get("task_switches") or {}
    if sw.get("count") and sw["weighted"] >= total * .1:
        kept = sw.get("compacted_to") or 30_000
        avg = sw["context_tokens"] / sw["count"]
        saved = sw["weighted"] * max(0.0, 1 - kept / avg) if avg else 0
        out.append({"id": "switches", "severity": "high", "stake": round(saved),
                    "title": f"{_pct(sw['weighted'], total)} of cost was earlier tasks carried into new ones",
                    "detail": f"{sw['count']} new branch{'es' if sw['count'] != 1 else ''} started with {_tk(avg)} of "
                              f"context on average. /compact at each one would have kept about {_tk(kept)} and saved "
                              f"about {_tk(saved)}.", "action": {"label": "Sessions", "panel": "waste:switches"},
                    "agent": "When the work moves to a new branch in a long session, tell the user /compact first "
                             "would cut the cost of everything after it."})
    idle = next((c for c in (cx.get("rebuilds") or {}).get("by_cause", []) if c["cause"] == "idle"), None)
    if idle and idle["weighted"] >= total * .05:
        out.append({"id": "idle", "severity": "medium", "stake": idle["weighted"],
                    "title": f"Cache rebuilds after idle were {_pct(idle['weighted'], total)} of cost",
                    "detail": f"{idle['rebuilds']} time{'s' if idle['rebuilds'] != 1 else ''} a session came back after "
                              "the cache expired and wrote its whole context again. New work after a break is cheaper "
                              "in a new session.", "action": {"label": "Rebuilds", "panel": "waste:idle"}, "agent": None})
    cats = cx.get("categories") or []
    habits = [c for c in cats if c["category"] in AGENT_HABITS and c["weighted"] >= total * .05][:2]
    for c in habits:
        out.append({"id": f"cat:{c['category']}", "severity": "medium", "stake": c["weighted"],
                    "title": f"{CATEGORY_TITLES[c['category']]} are {_pct(c['weighted'], total)} of cost",
                    "detail": f"{c['calls']} calls added {_tk(c['tokens'])} tokens, and the requests after them read "
                              f"those again as {_tk(c['carried_tokens'])} cache-read tokens.",
                    "action": {"label": "Details", "panel": f"cat:{c['category']}"},
                    "agent": f"{_pct(c['weighted'], total)} of cost here is {KEPT[c['category']]} kept in context: "
                             f"{AGENT_HABITS[c['category']]}."})
    system = next((c for c in cats if c["category"] == "system"), None)
    if system and system["calls"] and system["weighted"] >= total * .2:
        base = system["tokens"] / system["calls"]
        out.append({"id": "system", "severity": "medium", "stake": system["weighted"],
                    "title": f"Every request starts with about {_tk(base)} tokens",
                    "detail": f"The system prompt, tool definitions and CLAUDE.md are {_pct(system['weighted'], total)} of "
                              "cost. A shorter CLAUDE.md and fewer MCP servers cut every request.",
                    "action": {"label": "Details", "panel": "cat:system"}, "agent": None})
    prs = [p for p in use.get("by_pr", []) if p["weighted"]]
    if len(prs) >= 5:
        mid = median(p["weighted"] for p in prs)
        for p in sorted(prs, key=lambda p: -p["weighted"])[:2]:
            if p["weighted"] >= 3 * mid:
                out.append({"id": f"pr:{p['number']}", "severity": "low", "stake": p["weighted"] - mid,
                            "title": f"#{p['number']} cost {p['weighted'] / mid:.1f}x the median pull request",
                            "detail": f"{p['title']}: {_tk(p['weighted'])} over {p['sessions']} session"
                                      f"{'s' if p['sessions'] != 1 else ''}.",
                            "action": {"label": "Open", "panel": f"pr:{p['number']}"}, "agent": None})
    return out


def brief(conn: sqlite3.Connection, days: int = 30, repo: str | None = None, subagent: bool = False,
          found: list[dict[str, Any]] | None = None) -> str:
    """A few lines for an agent starting work: what to leave alone and what to do differently. Empty when there's
    nothing worth saying."""
    found = insights(conn, days) if found is None else found
    lines = []
    red = [i["red"] for i in found if i["id"].startswith("red:")]
    if red:
        items = ", ".join(f"{_name(t['test_id'])} (since {str(t['since_sha'])[:8]})" for t in red[:5])
        lines.append(f"- Failing on the default branch, so not caused by your branch: {items}"
                     f"{f' and {len(red) - 5} more' if len(red) > 5 else ''}. Report these; don't fix them in other work.")
    flaky = sorted((s for s in analysis.flake_stats(conn, days).values() if s["flip_shas"] > 0 and not s["quarantined"]),
                   key=lambda s: (-s["flake_score"], -s["flip_shas"]))
    if flaky:
        items = ", ".join(f"{_name(s['test_id'])} ({s['flip_shas']}/{s['eligible_shas']} commits)" for s in flaky[:8])
        lines.append(f"- Flaky, passed and failed on the same code: {items}"
                     f"{f' and {len(flaky) - 8} more' if len(flaky) > 8 else ''}. If one fails, rerun only it once; "
                     "don't debug it.")
    quarantined = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
    if quarantined:
        lines.append(f"- {quarantined} quarantined test{'s' if quarantined != 1 else ''}: the gate ignores "
                     f"{'its' if quarantined == 1 else 'their'} failures.")
    if lines:
        lines.append("- After a test run with failures, let the gate judge them before rerunning or debugging "
                     "(greenlight run, greenlight playtest gate, or the greenlight_triage_run tool).")
    # a subagent can't talk to the user, so it only gets habits it can change itself
    habits = [i["agent"] for i in found if i["agent"] and (not subagent or i["id"].startswith("cat:"))][:1 if subagent else 3]
    lines += [f"- {h}" for h in habits]
    if not lines:
        return ""
    head = f"greenlight{f' ({repo})' if repo else ''}, from the last {days} days of test runs and sessions:"
    return "\n".join([head, *lines])
