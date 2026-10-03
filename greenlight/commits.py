"""Commits and merges, and the Claude Code conversations they came from.

A conversation is a Claude Code session. Its usage hook sends the commits it made and the pull requests it merged
(agent_commits, exact: see usage.commits). Everything else joins on the commit: test runs (their git commit, or the
sha their code identity starts with), CI runs, pull requests (head and merge commits) and deployments. A short sha,
like a test ledger's, is matched to the one full sha it starts. A session that ran the tests on a commit is linked
to it too, as the one that tested it.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import analysis, usage
from .db import since

HOW = {"made": 0, "merged": 1, "tested": 2, "worked": 3}  # the strongest link wins
FAILED = {"failure", "timed_out", "startup_failure"}
LIMIT = 500


def _sha(code_id: str | None) -> str:
    """The commit in a code identity: <sha>, <sha>+<hash> (uncommitted edits) or <sha>@<matrix entry>."""
    return re.split(r"[+@]", str(code_id or ""), maxsplit=1)[0].strip().lower()


def _ids(meta: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Every id a run can carry for a session (Claude Code's, or the claude.ai one) -> the session."""
    out = {}
    for sid, m in meta.items():
        out[sid] = sid
        if m.get("remote_session"):
            out[m["remote_session"]] = sid
    return out


def conversation(meta: dict[str, dict[str, Any]], sid: str, how: str | None = None) -> dict[str, Any]:
    m = meta.get(sid, {})
    return {"session_id": sid, "remote_session": m.get("remote_session"), "title": m.get("title"), "url": m.get("url"),
            **({"how": how} if how else {})}


def repo_url(conn: sqlite3.Connection) -> str | None:
    """https://github.com/owner/repo, from a synced pull request or CI run, to link a commit."""
    for (url,) in conn.execute("SELECT url FROM pull_requests WHERE url IS NOT NULL UNION ALL "
                               "SELECT url FROM pipelines WHERE url IS NOT NULL LIMIT 20"):
        m = re.match(r"(https://github\.com/[^/]+/[^/]+)/(?:pull|actions)/", url or "")
        if m:
            return m.group(1)
    return None


def _gather(conn: sqlite3.Connection, start: str, only: str | None = None) -> tuple[dict[str, dict[str, Any]], dict]:
    """Every commit seen since start (or the one `only` names), with what touched it."""
    meta = usage._meta(conn)
    ids = _ids(meta)
    rows: dict[str, dict[str, Any]] = {}
    only = only.lower()[:7] if only else None  # a ledger's short sha has to come along: commit_detail picks the row
    like = only + "%" if only else None

    def row(sha: str, at: str | None, branch: str | None = None) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
            return None
        r = rows.setdefault(sha, {"sha": sha, "at": at, "branch": branch or "", "kind": "commit", "pr": None,
                                  "how": {}, "merged_by": None, "runs": [], "ci": [], "deploys": [], "made_at": None})
        if at and (not r["at"] or at < r["at"]):
            r["at"] = at
        if branch and not r["branch"]:
            r["branch"] = branch
        return r

    def link(r: dict[str, Any] | None, sid: str, how: str) -> None:
        if r is not None and (sid not in r["how"] or HOW[how] < HOW[r["how"][sid]]):
            r["how"][sid] = how

    cond = lambda col: (f" AND {col} LIKE ?", (like,)) if like else ("", ())  # noqa: E731
    prs = {p["number"]: p for p in map(dict, conn.execute(
        "SELECT number, title, state, head, base, head_sha, merge_sha, created_at, merged_at, closed_at, url "
        "FROM pull_requests"))}
    where, args = (" AND (sha LIKE ? OR sha = '')", (like,)) if like else ("", ())
    for a in conn.execute(f"SELECT * FROM agent_commits WHERE at >= ?{where}", (start, *args)):
        sha = a["sha"]
        if not sha and a["kind"] == "merge":  # gh pr merge: the pull request says which commit
            p = prs.get(a["pr"]) or next((p for p in prs.values() if p["head"] == a["branch"] and p["merge_sha"]
                                          and (p["merged_at"] or "") >= a["at"][:10]), None)
            sha = ((p or {}).get("merge_sha") or "").lower()
            if only and not sha.startswith(only):
                continue
        r = row(sha.lower(), a["at"], a["branch"])
        if r is None:
            continue
        if a["kind"] == "merge":  # a merge commit lands on the pull request's base, not the session's branch
            r["kind"], r["pr"], r["merged_by"] = "merge", r["pr"] or a["pr"], a["session_id"]
            r["branch"] = (prs.get(r["pr"]) or {}).get("base") or r["branch"]
            link(r, a["session_id"], "merged")
        else:
            r["made_at"] = min(filter(None, (r["made_at"], a["at"])))
            link(r, a["session_id"], "made")
    where, args = cond("COALESCE(git_commit, commit_sha)")
    for x in conn.execute(f"SELECT run_id, commit_sha, git_commit, branch, started_at, session, source, attempt "
                          f"FROM runs WHERE started_at >= ?{where}", (start, *args)):
        r = row(_sha(x["git_commit"] or x["commit_sha"]), x["started_at"], x["branch"])
        if r is not None:
            r["runs"].append(dict(x))
            if x["session"] in ids:
                link(r, ids[x["session"]], "tested")
    where, args = cond("commit_sha")
    for x in conn.execute(f"SELECT pipeline_id, workflow, run_number, attempt, status, branch, commit_sha, created_at, url "
                          f"FROM pipelines WHERE created_at >= ?{where}", (start, *args)):
        r = row(_sha(x["commit_sha"]), x["created_at"], x["branch"])
        if r is not None:
            r["ci"].append(dict(x))
    for p in prs.values():
        if p["merge_sha"] and (p["merged_at"] or "") >= start and (not only or p["merge_sha"].lower().startswith(only)):
            r = row(p["merge_sha"].lower(), p["merged_at"], p["base"])
            if r is not None:
                r["kind"], r["pr"], r["at"] = "merge", p["number"], p["merged_at"]
                r["branch"] = p["base"] or r["branch"]
    where, args = cond("c.commit_sha")
    for x in conn.execute(f"SELECT c.commit_sha, c.authored_at, d.environment, d.version, d.deployed_at, d.url "
                          f"FROM deploy_commits c JOIN deployments d USING (deploy_id) WHERE d.deployed_at >= ?{where}",
                          (start, *args)):
        r = row(_sha(x["commit_sha"]), x["authored_at"])
        if r is not None:
            r["deploys"].append({k: x[k] for k in ("environment", "version", "deployed_at", "url")})
    # a short sha (a test ledger's) joins the one full sha it starts
    full = sorted(s for s in rows if len(s) == 40)
    for short in [s for s in rows if len(s) < 40]:
        match = [s for s in full if s.startswith(short)]
        if len(match) == 1:
            r, into = rows.pop(short), rows[match[0]]
            for k in ("runs", "ci", "deploys"):
                into[k] += r[k]
            for sid, how in r["how"].items():
                if sid not in into["how"] or HOW[how] < HOW[into["how"][sid]]:
                    into["how"][sid] = how
            into["at"] = min(filter(None, (into["at"], r["at"])), default=None)
            into["branch"] = into["branch"] or r["branch"]
            into["pr"] = into["pr"] or r["pr"]
            into["merged_by"] = into["merged_by"] or r["merged_by"]
    for p in prs.values():  # a pull request's head commit, when something else saw it
        head = (p["head_sha"] or "").lower()
        if head in rows and not rows[head]["pr"]:
            rows[head]["pr"] = p["number"]
    return rows, {"meta": meta, "prs": prs}


def _ci(runs: list[dict[str, Any]]) -> str | None:
    """A commit's CI: the latest attempt of each workflow; failed if any failed, running if any still runs."""
    latest: dict[str, dict[str, Any]] = {}
    for r in sorted(runs, key=lambda r: (r["created_at"], r["attempt"])):
        latest[r["workflow"]] = r
    status = {r["status"] for r in latest.values()}
    if not status:
        return None
    if status & FAILED:
        return "failure"
    if status & {"in_progress", "queued", "waiting", "requested", "pending"}:
        return "in_progress"
    return "success" if status <= {"success", "skipped", "neutral"} else sorted(status)[0]


def _finish(conn: sqlite3.Connection, r: dict[str, Any], ctx: dict, by_head: dict) -> dict[str, Any]:
    meta, prs = ctx["meta"], ctx["prs"]
    if not r["pr"] and r["branch"]:
        p = usage._pr_for(by_head, r["branch"], r["made_at"] or r["at"] or "")
        r["pr"] = p["number"] if p else None
    p = prs.get(r["pr"])
    runs = sorted(r["runs"], key=lambda x: (x["started_at"], x["run_id"]))
    decision = analysis.triage_run(conn, run_id=runs[-1]["run_id"])["decision"] if runs else None
    ci = sorted(r["ci"], key=lambda x: (x["created_at"], x["attempt"]))
    convs = sorted((conversation(meta, sid, how) for sid, how in r["how"].items()), key=lambda c: HOW[c["how"]])
    return {"sha": r["sha"], "at": r["made_at"] or r["at"], "branch": r["branch"], "kind": r["kind"],
            "pr": {k: p[k] for k in ("number", "title", "state", "head", "url")} if p else None,
            "conversations": convs, "merged_by": conversation(meta, r["merged_by"], "merged") if r["merged_by"] else None,
            "runs": len(runs), "run_id": runs[-1]["run_id"] if runs else None,
            "decision": decision, "ci": _ci(ci), "pipeline_id": ci[-1]["pipeline_id"] if ci else None,
            "deploys": sorted(r["deploys"], key=lambda d: d["deployed_at"])}


def commit_list(conn: sqlite3.Connection, days: int = 30, limit: int = LIMIT) -> dict[str, Any]:
    """Commits and merges in the window, newest first, each with its conversations, pull request, gate decision,
    CI and deployments; and the merged pull requests, each with its commits and everyone who worked on it."""
    start = since(days)
    rows, ctx = _gather(conn, start)
    _, by_head = usage._prs(conn)
    out = sorted((_finish(conn, r, ctx, by_head) for r in rows.values()), key=lambda r: r["at"] or "", reverse=True)
    return {"commits": out[:limit], "merges": _merges(conn, start, out, ctx, by_head), "repo_url": repo_url(conn)}


def _merges(conn: sqlite3.Connection, start: str, commits: list[dict[str, Any]], ctx: dict, by_head: dict) -> list[dict]:
    meta, prs = ctx["meta"], ctx["prs"]
    worked: dict[int, set[str]] = {}  # sessions that spent tokens on the pull request's branch while it was open
    heads = {p["head"] for p in prs.values() if p["head"]}
    if heads:
        marks = ",".join("?" * len(heads))
        for sid, branch, first in conn.execute(f"SELECT session_id, branch, MIN(minute) FROM agent_usage WHERE branch IN "
                                               f"({marks}) GROUP BY session_id, branch", sorted(heads)):
            p = usage._pr_for(by_head, branch, first)
            if p:
                worked.setdefault(p["number"], set()).add(sid)
    by_pr: dict[int, list[dict[str, Any]]] = {}
    for c in commits:
        if c["pr"]:
            by_pr.setdefault(c["pr"]["number"], []).append(c)
    out = []
    for p in prs.values():
        if p["state"] != "merged" or (p["merged_at"] or "") < start:
            continue
        mine = by_pr.get(p["number"], [])
        how: dict[str, str] = {sid: "worked" for sid in worked.get(p["number"], ())}
        for c in mine:
            for conv in c["conversations"]:
                if conv["session_id"] not in how or HOW[conv["how"]] < HOW[how[conv["session_id"]]]:
                    how[conv["session_id"]] = conv["how"]
        head = next((c for c in mine if p["head_sha"] and c["sha"] == p["head_sha"].lower()), None)
        merge = next((c for c in mine if c["merged_by"]), None) or next((c for c in mine if c["kind"] == "merge"), None)
        out.append({k: p[k] for k in ("number", "title", "head", "base", "merged_at", "url", "merge_sha")}
                   | {"commits": sum(c["kind"] == "commit" for c in mine),
                      "conversations": sorted((conversation(meta, sid, h) for sid, h in how.items()),
                                              key=lambda c: HOW[c["how"]]),
                      "merged_by": (merge or {}).get("merged_by"),
                      "ci": (head or merge or {}).get("ci"), "decision": (head or merge or {}).get("decision")})
    return sorted(out, key=lambda m: m["merged_at"], reverse=True)


def commit_detail(conn: sqlite3.Connection, sha: str) -> dict[str, Any]:
    """One commit for its side panel: who made, merged or tested it, its test runs with the gate's decision, its CI
    runs, its pull request and where it shipped."""
    sha = sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("a commit is 7 to 40 hex characters")
    rows, ctx = _gather(conn, "0", only=sha)
    r = rows.get(sha) or next((v for k, v in rows.items() if k.startswith(sha)), None)
    if not r:
        raise LookupError(f"Nothing recorded for commit {sha}")
    _, by_head = usage._prs(conn)
    out = _finish(conn, r, ctx, by_head)
    ids = _ids(ctx["meta"])
    runs = []
    for x in sorted(r["runs"], key=lambda x: (x["started_at"], x["run_id"]), reverse=True)[:30]:
        t = analysis.triage_run(conn, run_id=x["run_id"])
        runs.append(x | {"decision": t["decision"], "failing": len(t["failures"]), "tests": t["run"]["tests"],
                         "conversation": conversation(ctx["meta"], ids[x["session"]]) if x["session"] in ids else None})
    return out | {"repo_url": repo_url(conn), "test_runs": runs,
                  "ci_runs": sorted(r["ci"], key=lambda x: (x["created_at"], x["attempt"]), reverse=True)[:30]}


def for_pr(conn: sqlite3.Connection, number: int, days: int) -> dict[str, Any]:
    """A pull request's commits in the window, and the conversation that merged it."""
    out = commit_list(conn, days)
    merge = next((m for m in out["merges"] if m["number"] == number), None)
    return {"commits": [c for c in out["commits"] if c["pr"] and c["pr"]["number"] == number],
            "merged_by": (merge or {}).get("merged_by"), "repo_url": out["repo_url"]}


def for_session(conn: sqlite3.Connection, sid: str) -> list[dict[str, Any]]:
    """The commits a session made and the pull requests it merged, newest first."""
    prs = {p["number"]: p for p in map(dict, conn.execute("SELECT number, title, state, head, merge_sha, url FROM pull_requests"))}
    _, by_head = usage._prs(conn)
    out = []
    for a in conn.execute("SELECT * FROM agent_commits WHERE session_id = ? ORDER BY at DESC", (sid,)):
        p = prs.get(a["pr"]) or usage._pr_for(by_head, a["branch"], a["at"])
        sha = a["sha"] or (p or {}).get("merge_sha") or ""
        out.append({"sha": sha.lower(), "kind": a["kind"], "at": a["at"], "branch": a["branch"],
                    "pr": {k: p[k] for k in ("number", "title", "state", "url")} if p else None})
    return out


def by_sha(conn: sqlite3.Connection, shas: list[str]) -> dict[str, dict[str, Any]]:
    """The conversation that made (else merged) each commit, for lists that show one per row."""
    shas = sorted({_sha(s) for s in shas if s})
    if not shas:
        return {}
    meta = usage._meta(conn)
    out: dict[str, dict[str, Any]] = {}
    marks = ",".join("?" * len(shas))
    for sid, sha, kind in conn.execute(f"SELECT session_id, sha, kind FROM agent_commits WHERE sha IN ({marks}) "
                                       "ORDER BY kind = 'merge', at", shas):
        out.setdefault(sha, conversation(meta, sid, "made" if kind == "commit" else "merged"))
    return out


def for_runs(conn: sqlite3.Connection, runs: list[dict[str, Any]]) -> None:
    """Each run's conversation: the session that recorded it, else the one that made its commit."""
    meta = usage._meta(conn)
    ids = _ids(meta)
    made = by_sha(conn, [r.get("git_commit") or r.get("commit_sha") for r in runs])
    for r in runs:
        sid = ids.get(r.get("session") or "")
        r["conversation"] = (conversation(meta, sid, "tested") if sid
                             else made.get(_sha(r.get("git_commit") or r.get("commit_sha"))))
