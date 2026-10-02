"""Pull GitHub data into the local DB: pull requests, issues, Actions runs (with jobs, steps and
JUnit artifacts) and deployments. Every function is incremental and safe to rerun."""
from __future__ import annotations

import fnmatch
import io
import json
import re
import sqlite3
import zipfile
from collections import defaultdict
from datetime import timedelta
from typing import Any

from .db import iso, norm_time, parse_time, since, utcnow
from .github import GitHub, GitHubError
from .gitrepo import Repo
from .ingest import assign_retries, parse_junit, record_run

MARKER = re.compile(r"<!--\s*flakewatch:(flaky|perf):(.+?)\s*-->")
FINISHED = {"success", "failure", "cancelled", "skipped", "timed_out", "neutral", "action_required", "stale",
            "startup_failure"}
FAILED = {"failure", "timed_out", "startup_failure"}
ACTIVITY_TYPES = {"push", "force_push", "pr_merge", "merge_queue_merge", "branch_creation"}


def _cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,)).fetchone()
    return row["cursor"] if row else None


def _save_cursor(conn: sqlite3.Connection, source: str, cursor: str | None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO sync_state (source, cursor, synced_at) VALUES (?, ?, ?)",
                     (source, cursor, iso(utcnow())))


def _ms(start: str | None, end: str | None) -> int | None:
    a, b = parse_time(start), parse_time(end)
    if not a or not b:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


# ---------- pull requests ----------
def sync_pulls(conn: sqlite3.Connection, gh: GitHub, days: int = 90) -> dict[str, int]:
    """Newest-updated first, stopping at the last sync's high-water mark (or `days` back)."""
    stop = _cursor(conn, "pulls") or since(days)
    newest, n = stop, 0
    for pr in gh.paginate("/pulls", {"state": "all", "sort": "updated", "direction": "desc"}):
        updated = norm_time(pr.get("updated_at"))
        if updated and updated < stop:
            break
        merged = norm_time(pr.get("merged_at"))
        state = "merged" if merged else pr.get("state", "open")
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO pull_requests (number, title, author, state, draft, base, head, head_sha,
                   merge_sha, created_at, merged_at, closed_at, updated_at, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pr["number"], pr.get("title"), (pr.get("user") or {}).get("login"), state, int(bool(pr.get("draft"))),
                 (pr.get("base") or {}).get("ref"), (pr.get("head") or {}).get("ref"), (pr.get("head") or {}).get("sha"),
                 pr.get("merge_commit_sha") if merged else None, norm_time(pr.get("created_at")), merged,
                 norm_time(pr.get("closed_at")), updated, pr.get("html_url")))
        n += 1
        newest = max(newest, updated or newest)
    _save_cursor(conn, "pulls", newest)
    return {"pull_requests": n}


# ---------- issues ----------
def fw_key(body: str | None) -> str | None:
    m = MARKER.search(body or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def upsert_issue(conn: sqlite3.Connection, it: dict[str, Any]) -> None:
    labels = [lb["name"] if isinstance(lb, dict) else str(lb) for lb in it.get("labels") or []]
    conn.execute(
        """INSERT OR REPLACE INTO issues (number, title, state, state_reason, labels, author, created_at, closed_at,
           updated_at, comments, fw_key, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (it["number"], it.get("title"), it.get("state", "open"), it.get("state_reason"), json.dumps(labels),
         (it.get("user") or {}).get("login"), norm_time(it.get("created_at")), norm_time(it.get("closed_at")),
         norm_time(it.get("updated_at")), it.get("comments"), fw_key(it.get("body")), it.get("html_url")))


def sync_issues(conn: sqlite3.Connection, gh: GitHub, quarantine_label: str = "quarantined",
                days: int = 90) -> dict[str, int]:
    start = _cursor(conn, "issues") or since(days)
    newest, n = start, 0
    for it in gh.paginate("/issues", {"state": "all", "since": start, "sort": "updated", "direction": "asc"}):
        if "pull_request" in it:  # the issues endpoint lists PRs too
            continue
        with conn:
            upsert_issue(conn, it)
        n += 1
        newest = max(newest, norm_time(it.get("updated_at")) or newest)
    _save_cursor(conn, "issues", newest)
    return {"issues": n, "quarantined_by_label": mirror_issue_quarantine(conn, quarantine_label)}


def mirror_issue_quarantine(conn: sqlite3.Connection, label: str) -> int:
    """An open flaky-test issue carrying `label` quarantines its test. Remove the label or close the
    issue and the next sync releases it. Only rows this mirror added ("github#<n>") are touched."""
    wanted: dict[str, tuple[int, str]] = {}
    for r in conn.execute("SELECT number, title, labels, fw_key FROM issues WHERE state = 'open' AND fw_key LIKE 'flaky:%'"):
        if label in json.loads(r["labels"] or "[]"):
            wanted[r["fw_key"].split(":", 1)[1]] = (r["number"], r["title"] or "")
    with conn:
        for row in conn.execute("SELECT test_id, added_by FROM quarantine WHERE added_by LIKE 'github#%'").fetchall():
            if row["test_id"] not in wanted:
                conn.execute("DELETE FROM quarantine WHERE test_id = ?", (row["test_id"],))
        for test_id, (number, title) in wanted.items():
            conn.execute(
                "INSERT INTO quarantine (test_id, reason, added_at, added_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(test_id) DO UPDATE SET reason = excluded.reason, added_by = excluded.added_by "
                "WHERE quarantine.added_by LIKE 'github#%'",
                (test_id, f"labeled {label} on issue #{number}", iso(utcnow()), f"github#{number}"))
    return len(wanted)


# ---------- GitHub Actions ----------
def _job_rows(pipeline_id: str, jobs: list[dict[str, Any]]) -> tuple[list[tuple], list[tuple]]:
    job_rows, step_rows = [], []
    for j in jobs:
        jid = f"gha:{j['id']}"
        status = j.get("conclusion") or j.get("status") or "unknown"
        job_rows.append((jid, pipeline_id, j.get("name") or "job", status, norm_time(j.get("started_at")),
                         norm_time(j.get("completed_at")), _ms(j.get("started_at"), j.get("completed_at")),
                         _ms(j.get("created_at"), j.get("started_at")), j.get("runner_name"), j.get("html_url")))
        for s in j.get("steps") or []:
            step_rows.append((jid, s.get("number", 0), s.get("name") or "step",
                              s.get("conclusion") or s.get("status") or "unknown",
                              _ms(s.get("started_at"), s.get("completed_at"))))
    return job_rows, step_rows


def _attempt_status(jobs: list[dict[str, Any]]) -> str:
    states = [j.get("conclusion") or j.get("status") for j in jobs]
    if any(s in ("in_progress", "queued", "waiting", "pending") for s in states):
        return "in_progress"
    for bad in ("failure", "timed_out", "cancelled"):
        if bad in states:
            return bad
    return "success" if states else "unknown"


def _store_pipeline(conn: sqlite3.Connection, run: dict[str, Any], attempt: int, status: str,
                    started: str | None, finished: str | None, jobs: list[dict[str, Any]]) -> None:
    pid = f"gha:{run['id']}:{attempt}"
    job_starts = [j.get("started_at") for j in jobs if j.get("started_at")]
    queue = _ms(started, min(job_starts)) if job_starts and started else None
    if attempt == 1 and run.get("created_at") and started:
        queue = _ms(run["created_at"], min(job_starts)) if job_starts else _ms(run["created_at"], started)
    done = status in FINISHED
    conn.execute(
        """INSERT OR REPLACE INTO pipelines (pipeline_id, provider, workflow, run_number, attempt, event, branch,
           commit_sha, status, created_at, started_at, finished_at, duration_ms, queue_ms, actor, url)
           VALUES (?, 'github-actions', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, run.get("name") or run.get("display_title") or "workflow", run.get("run_number"), attempt,
         run.get("event"), run.get("head_branch"), run.get("head_sha"), status, norm_time(run.get("created_at")),
         norm_time(started), norm_time(finished) if done else None, _ms(started, finished) if done else None,
         queue, (run.get("triggering_actor") or run.get("actor") or {}).get("login"),
         f"{run.get('html_url')}/attempts/{attempt}" if run.get("html_url") else None))
    job_rows, step_rows = _job_rows(pid, jobs)
    conn.execute("DELETE FROM jobs WHERE pipeline_id = ?", (pid,))
    conn.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", job_rows)
    conn.executemany("INSERT OR REPLACE INTO steps VALUES (?, ?, ?, ?, ?)", step_rows)


def sync_actions(conn: sqlite3.Connection, gh: GitHub, days: int = 30, junit_glob: str | None = "junit*",
                 max_runs: int = 1000) -> dict[str, Any]:
    """Workflow runs created in the last `days` (or since the last sync, less a day for runs that were
    still going), every attempt with its jobs and steps, and JUnit reports from matching artifacts."""
    last = _cursor(conn, "actions")
    start = (parse_time(last) - timedelta(days=1)) if last else utcnow() - timedelta(days=days)
    done_ids = {r[0] for r in conn.execute(
        "SELECT pipeline_id FROM pipelines WHERE provider = 'github-actions' AND finished_at IS NOT NULL")}
    runs = pipelines = tests = 0
    notes: list[str] = []
    for run in gh.paginate("/actions/runs", {"created": f">={start.date().isoformat()}"}, key="workflow_runs",
                           limit=max_runs):
        runs += 1
        latest = int(run.get("run_attempt") or 1)
        status = run.get("conclusion") or run.get("status") or "unknown"
        if f"gha:{run['id']}:{latest}" in done_ids and status in FINISHED:
            continue
        jobs = list(gh.paginate(f"/actions/runs/{run['id']}/jobs", {"filter": "all"}, key="jobs"))
        by_attempt: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for j in jobs:
            by_attempt[int(j.get("run_attempt") or latest)].append(j)
        with conn:
            for attempt in range(1, latest + 1):
                aj = by_attempt.get(attempt, [])
                if attempt == latest:
                    _store_pipeline(conn, run, attempt, status, run.get("run_started_at"),
                                    run.get("updated_at"), aj)
                elif aj:
                    starts = [j["started_at"] for j in aj if j.get("started_at")]
                    ends = [j["completed_at"] for j in aj if j.get("completed_at")]
                    _store_pipeline(conn, run, attempt, _attempt_status(aj), min(starts) if starts else None,
                                    max(ends) if ends else None, aj)
                pipelines += 1
        if junit_glob and status in FINISHED:
            try:
                tests += _ingest_artifacts(conn, gh, run, junit_glob)
            except GitHubError as e:
                notes.append(f"run {run['id']}: {e}")
    _save_cursor(conn, "actions", iso(utcnow()))
    return {"workflow_runs": runs, "pipelines_stored": pipelines, "junit_runs": tests, "notes": notes[:10]}


def _ingest_artifacts(conn: sqlite3.Connection, gh: GitHub, run: dict[str, Any], pattern: str) -> int:
    added = 0
    for art in gh.paginate(f"/actions/runs/{run['id']}/artifacts", key="artifacts"):
        if art.get("expired") or not fnmatch.fnmatch(art.get("name", ""), pattern):
            continue
        ext = f"gha-artifact:{art['id']}"
        if conn.execute("SELECT 1 FROM runs WHERE external_id = ?", (ext,)).fetchone():
            continue
        blob = gh.download(f"/actions/artifacts/{art['id']}/zip")
        results = []
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for info in z.infolist():
                if info.filename.lower().endswith(".xml") and info.file_size < 32 * 1024 * 1024:
                    try:
                        results.extend(parse_junit(io.BytesIO(z.read(info))))
                    except Exception:  # noqa: BLE001 - one unreadable file shouldn't drop the rest
                        continue
        if not results:
            continue
        m = re.search(r"attempt-(\d+)", art.get("name", ""))
        record_run(conn, assign_retries(results), commit_sha=run.get("head_sha"), branch=run.get("head_branch"),
                   attempt=int(m.group(1)) if m else None, source="ci", external_id=ext,
                   started_at=parse_time(run.get("run_started_at") or run.get("created_at")),
                   session=f"{run.get('name')} #{run.get('run_number')}", url=run.get("html_url"))
        added += 1
    return added


# ---------- deployments ----------
def _store_deploy(conn: sqlite3.Connection, deploy_id: str, source: str, env: str, sha: str, prev: str | None,
                  at: str, status: str = "success", version: str | None = None, url: str | None = None) -> bool:
    exists = conn.execute("SELECT status FROM deployments WHERE deploy_id = ?", (deploy_id,)).fetchone()
    if exists and exists["status"] == status:
        return False
    conn.execute("""INSERT OR REPLACE INTO deployments (deploy_id, source, environment, commit_sha, previous_sha,
                    deployed_at, status, version, url) VALUES (?,?,?,?,?,?,?,?,?)""",
                 (deploy_id, source, env, sha, prev, norm_time(at), status, version, url))
    return not exists


def sync_branch_pushes(conn: sqlite3.Connection, gh: GitHub, branch: str) -> int:
    """Every change to `branch` (push, merge, force push) is a deployment to an environment named for it.
    Uses the repository activity API, which records when each push happened."""
    n = 0
    with conn:
        for a in gh.paginate("/activity", {"ref": f"refs/heads/{branch}", "direction": "desc"}):
            if a.get("activity_type") not in ACTIVITY_TYPES or not a.get("after") or set(a["after"]) == {"0"}:
                continue
            before = a.get("before")
            before = None if not before or set(before) == {"0"} else before
            n += _store_deploy(conn, f"activity:{a['id']}", "branch-push", branch, a["after"], before,
                               a["timestamp"], url=f"https://github.com/{gh.repo}/commit/{a['after']}")
    return n


def sync_gh_deployments(conn: sqlite3.Connection, gh: GitHub, environment: str, limit: int = 300) -> int:
    n = 0
    for d in gh.paginate("/deployments", {"environment": environment}, limit=limit):
        did = f"gh-deployment:{d['id']}"
        row = conn.execute("SELECT status FROM deployments WHERE deploy_id = ?", (did,)).fetchone()
        if row and row["status"] in ("success", "failure"):
            continue
        statuses = gh.get(f"/deployments/{d['id']}/statuses", {"per_page": 1}) or []
        state = statuses[0]["state"] if statuses else "pending"
        status = "success" if state in ("success", "inactive") else "failure" if state in ("failure", "error") else state
        with conn:
            n += _store_deploy(conn, did, "github-deployment", environment, d["sha"], None,
                               (statuses[0].get("created_at") if statuses else None) or d["created_at"], status,
                               d.get("ref"), statuses[0].get("environment_url") if statuses else None)
    return n


def sync_releases(conn: sqlite3.Connection, gh: GitHub, repo: Repo | None) -> int:
    n = 0
    for r in gh.paginate("/releases"):
        if r.get("draft") or not r.get("published_at"):
            continue
        did = f"gh-release:{r['id']}"
        if conn.execute("SELECT 1 FROM deployments WHERE deploy_id = ?", (did,)).fetchone():
            continue
        tag = r.get("tag_name")
        sha = repo.resolve(f"refs/tags/{tag}") if repo else None
        if not sha:
            try:
                sha = (gh.get(f"/commits/{tag}") or {}).get("sha")
            except GitHubError:
                sha = None
        if not sha:
            continue
        with conn:
            n += _store_deploy(conn, did, "github-release", "production", sha, None, r["published_at"],
                               version=tag, url=r.get("html_url"))
    return n


def sync_deploy_files(conn: sqlite3.Connection, repo: Repo, pattern: str, ref: str | None = None,
                      environment: str = "production") -> int:
    """Each file matching `pattern` added on `ref` (first-parent) marks a release at that commit, e.g.
    patch notes named docs/patch-notes/2026-10-02-vesper-hale.md."""
    ref = ref or repo.default_branch()
    n = 0
    with conn:
        for sha, when, path in repo.file_additions(ref, pattern):
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            n += _store_deploy(conn, f"git-file:{path}", "git-file", environment, sha, None, when, version=stem)
    return n


def link_deploy_commits(conn: sqlite3.Connection, gh: GitHub | None, repo: Repo | None) -> int:
    """Fill deploy_commits: what each deployment shipped since the one before it in the same
    environment. The first deployment of an environment gets only its own commit."""
    added = 0
    deploys = conn.execute("""SELECT deploy_id, environment, commit_sha, previous_sha, deployed_at FROM deployments
                              WHERE status = 'success' ORDER BY environment, deployed_at""").fetchall()
    done = {r[0] for r in conn.execute("SELECT DISTINCT deploy_id FROM deploy_commits")}
    prev_by_env: dict[str, str] = {}
    for d in deploys:
        base = d["previous_sha"] or prev_by_env.get(d["environment"])
        prev_by_env[d["environment"]] = d["commit_sha"]
        if d["deploy_id"] in done:
            continue
        commits: list[tuple[str, str]] = []
        if base == d["commit_sha"]:
            pass
        elif repo and repo.resolve(d["commit_sha"]) and (base is None or repo.resolve(base)):
            commits = repo.commits_between(base, d["commit_sha"], limit=1 if base is None else 2000)
        elif gh and base:
            try:
                cmp = gh.get(f"/compare/{base}...{d['commit_sha']}")
                commits = [(c["sha"], (c.get("commit") or {}).get("author", {}).get("date"))
                           for c in (cmp or {}).get("commits", [])]
            except GitHubError:
                commits = []
        elif gh:
            try:
                c = gh.get(f"/commits/{d['commit_sha']}")
                commits = [(c["sha"], c["commit"]["author"]["date"])]
            except (GitHubError, KeyError, TypeError):
                commits = []
        rows = [(d["deploy_id"], sha, norm_time(when)) for sha, when in commits if when]
        with conn:
            conn.executemany("INSERT OR IGNORE INTO deploy_commits VALUES (?, ?, ?)", rows)
        added += len(rows)
    return added


def sync_deployments(conn: sqlite3.Connection, gh: GitHub | None, repo: Repo | None,
                     delivery: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if delivery.get("deploy_branch") and gh:
        out["branch_pushes"] = sync_branch_pushes(conn, gh, delivery["deploy_branch"])
    if delivery.get("deploy_environment") and gh:
        out["github_deployments"] = sync_gh_deployments(conn, gh, delivery["deploy_environment"])
    if delivery.get("deploy_releases") and gh:
        out["releases"] = sync_releases(conn, gh, repo)
    if delivery.get("deploy_files") and repo:
        out["deploy_files"] = sync_deploy_files(conn, repo, delivery["deploy_files"], delivery.get("deploy_files_ref"))
    if out:
        out["commits_linked"] = link_deploy_commits(conn, gh, repo)
    return out


def latest_sync(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["source"]: r["synced_at"] for r in conn.execute("SELECT source, synced_at FROM sync_state")}

