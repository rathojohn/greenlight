"""greenlight in CI: record a job's results, gate on them, and report on the pull request.

Jobs start with an empty disk, so the history lives on a greenlight server (`greenlight serve`): with
GREENLIGHT_URL and GREENLIGHT_TOKEN set, `report` sends the job's run there and uses its decision. Without
a server, the job is judged on its own results alone.

  greenlight ci record --junit 'reports/*.xml' [--name py3.12]
  greenlight ci report [--pr-comment] [--name py3.12]  step summary, outputs, PR comment
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import analysis
from .db import parse_time
from .github import GitHub, GitHubError
from .ingest import TestResult, assign_retries, parse_junit, record_run

RECORD_VERSION = 1


def actions_env() -> dict[str, Any]:
    """What GitHub Actions tells a step about the run. Empty values when run elsewhere."""
    e = os.environ
    pr = None
    path = e.get("GITHUB_EVENT_PATH")
    if path and Path(path).is_file():
        try:
            event = json.loads(Path(path).read_text(encoding="utf-8"))
            pr = (event.get("pull_request") or {}).get("number")
        except ValueError:
            pr = None
    server, repo, run_id = e.get("GITHUB_SERVER_URL", "https://github.com"), e.get("GITHUB_REPOSITORY"), e.get("GITHUB_RUN_ID")
    attempt = e.get("GITHUB_RUN_ATTEMPT") or "1"
    return {
        "in_actions": e.get("GITHUB_ACTIONS") == "true",
        "sha": e.get("GITHUB_SHA"),
        "branch": e.get("GITHUB_HEAD_REF") or e.get("GITHUB_REF_NAME"),
        "repo": repo,
        "run_id": run_id,
        "attempt": attempt,
        "workflow": e.get("GITHUB_WORKFLOW"),
        "job": e.get("GITHUB_JOB"),
        "pr": pr,
        "url": f"{server}/{repo}/actions/runs/{run_id}/attempts/{attempt}" if repo and run_id else None,
    }


# ---------- records ----------
def export_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
    results = [[r["test_id"], r["file"], r["outcome"], r["duration_ms"], r["retry"], r["failure_sig"], r["message"],
                r["flags"]] for r in conn.execute("SELECT * FROM results WHERE run_id = ? ORDER BY test_id, retry", (run_id,))]
    metrics = [[r["test_id"], r["name"], r["value"]] for r in conn.execute(
        "SELECT * FROM metrics WHERE run_id = ? ORDER BY test_id, name", (run_id,))]
    keep = ("external_id", "commit_sha", "branch", "source", "started_at", "duration_ms", "git_commit", "total_tests",
            "session", "command", "url")
    return {"greenlight_record": RECORD_VERSION, **{k: run[k] for k in keep}, "results": results, "metrics": metrics}


def load_record(conn: sqlite3.Connection, data: dict[str, Any]) -> bool:
    if data.get("greenlight_record") != RECORD_VERSION or not data.get("external_id"):
        return False
    results = [TestResult(t, f, o, d, retry=r, failure_sig=s, message=m, flags=fl)
               for t, f, o, d, r, s, m, fl in data.get("results", [])]
    _, created = record_run(
        conn, results, commit_sha=data["commit_sha"], branch=data.get("branch"), source=data.get("source"),
        external_id=data["external_id"], started_at=parse_time(data["started_at"]), duration_ms=data.get("duration_ms"),
        git_commit=data.get("git_commit"), total_tests=data.get("total_tests"), session=data.get("session"),
        command=data.get("command"), url=data.get("url"), metrics=[tuple(m) for m in data.get("metrics", [])])
    return created


def record_junit(conn: sqlite3.Connection, files: list[str], name: str | None = None, sha: str | None = None,
                 branch: str | None = None) -> dict[str, Any]:
    env = actions_env()
    results: list[TestResult] = []
    for f in files:
        results.extend(parse_junit(f))
    if not results:
        raise ValueError(f"No <testcase> elements in {len(files)} file(s). Is the reporter set to JUnit XML?")
    sha = sha or env["sha"]
    if not sha:
        raise ValueError("No commit: pass --sha, or run inside GitHub Actions")
    base = job_external_id(env, name) if env["run_id"] else f"local:{sha[:12]}{':' + name if name else ''}"
    # a job that records twice (say, after rerunning just the flaky tests) gets :2, :3... so the
    # rerun lands as a second attempt on the same commit, which is what flake detection needs
    ext, n = base, 1
    while conn.execute("SELECT 1 FROM runs WHERE external_id = ?", (ext,)).fetchone():
        n += 1
        ext = f"{base}:{n}"
    session = " / ".join(x for x in (env["workflow"], env["job"], name) if x) or None
    # a matrix entry is its own environment: a test failing on one Python and passing on another, on the
    # same commit, is a real difference, not a flake, so the name is part of what was tested
    code = f"{sha}@{name}" if name else sha
    run_id, created = record_run(conn, assign_retries(results), commit_sha=code, branch=branch or env["branch"],
                                 source="ci", external_id=ext, session=session, url=env["url"],
                                 git_commit=sha if name else None)
    return {"run_id": run_id, "created": created, "results": len(results), "external_id": ext}


def job_external_id(env: dict[str, Any], name: str | None) -> str:
    return f"gha:{env['run_id']}:{env['attempt']}:{env['job']}{':' + name if name else ''}"


def this_job_run(conn: sqlite3.Connection, name: str | None) -> int | None:
    """The newest run this Actions job recorded, so a report never picks up a sibling matrix job."""
    env = actions_env()
    if not env["run_id"]:
        return None
    base = job_external_id(env, name)
    row = conn.execute("SELECT run_id FROM runs WHERE external_id = ? OR substr(external_id, 1, ?) = ? "
                       "ORDER BY started_at DESC, run_id DESC LIMIT 1", (base, len(base) + 1, base + ":")).fetchone()
    return row["run_id"] if row else None


# ---------- reporting ----------
LABELS = {"PASS": "Pass", "RERUN_TARGETED": "Rerun the flaky tests only", "REAL_FAILURE": "Real failure"}
WHY = {
    "quarantined": "quarantined, ignored",
    "known_flaky": "known flaky",
    "suspect_flaky": "flipped once before",
    "new_test": "new test, no history",
    "real_failure": "no flake history",
}


def pr_marker(name: str | None) -> str:
    return f"<!-- greenlight:pr-report{':' + name if name else ''} -->"


def report_markdown(t: dict[str, Any], name: str | None = None, issue_links: dict[str, int] | None = None,
                    repo: str | None = None) -> str:
    links = issue_links or {}
    head = f"### greenlight{' (' + name + ')' if name else ''}: {LABELS[t['decision']]}"
    lines = [pr_marker(name), head, "", t["summary"], ""]
    if t["failures"]:
        lines += ["| Test | Verdict | Flipped on |", "| --- | --- | --- |"]
        order = {"real_failure": 0, "new_test": 1, "known_flaky": 2, "suspect_flaky": 3, "quarantined": 4}
        for f in sorted(t["failures"], key=lambda f: order.get(f["category"], 9)):
            issue = links.get(f["test_id"])
            ref = f" ([#{issue}](https://github.com/{repo}/issues/{issue}))" if issue and repo else f" (#{issue})" if issue else ""
            flips = f"{f['flip_shas']} of {f['eligible_shas']} commits" if f["eligible_shas"] else "n/a"
            lines.append(f"| `{f['test_id'].replace('|', '/')}`{ref} | {WHY.get(f['category'], f['category'])} | {flips} |")
        lines.append("")
    if t["rerun_tests"]:
        lines += ["Rerun only these:", "```", *t["rerun_tests"], "```", ""]
    if t["decision"] == "REAL_FAILURE":
        lines.append("A test with no flake history failed. This is the case that needs a fix, then a full run.")
    elif t["decision"] == "RERUN_TARGETED":
        lines.append("Only tests with a flake history failed. Rerun them, not the whole suite.")
    if t["passed_on_retry"]:
        lines.append(f"{len(t['passed_on_retry'])} test(s) passed only on retry.")
    return "\n".join(lines).rstrip() + "\n"


def issue_links(conn: sqlite3.Connection, test_ids: list[str]) -> dict[str, int]:
    if not test_ids:
        return {}
    keys = [f"flaky:{t}" for t in test_ids]
    rows = conn.execute(f"SELECT number, managed_key FROM issues WHERE managed_key IN ({','.join('?' * len(keys))})", keys)
    return {r["managed_key"].split(":", 1)[1]: r["number"] for r in rows}


def write_step_summary(markdown: str) -> bool:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(markdown + "\n")
    return True


def write_outputs(values: dict[str, Any]) -> bool:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")
    return True


def upsert_pr_comment(gh: GitHub, pr: int, body: str, name: str | None = None) -> dict[str, Any]:
    """One comment per job name, edited in place on every run instead of piling up."""
    mark = pr_marker(name)
    for c in gh.paginate(f"/issues/{pr}/comments"):
        if (c.get("body") or "").startswith(mark):
            return gh.patch(f"/issues/comments/{c['id']}", {"body": body})
    return gh.post(f"/issues/{pr}/comments", {"body": body})


def report(conn: sqlite3.Connection, run_id: int | None = None, sha: str | None = None, name: str | None = None,
           gh: GitHub | None = None, pr: int | None = None, window_days: int = analysis.DEFAULT_WINDOW_DAYS,
           triage: dict[str, Any] | None = None, links: dict[str, int] | None = None) -> dict[str, Any]:
    """Gate a run and write the step summary, outputs and PR comment. triage and links: a decision a
    greenlight server already made (it holds the history; this job's database only has this run)."""
    t = triage or analysis.triage_run(conn, run_id=run_id, commit_sha=sha, window_days=window_days)
    if links is None:
        links = issue_links(conn, [f["test_id"] for f in t["failures"]])
    md = report_markdown(t, name, links, gh.repo if gh else None)
    out = {"triage": t, "markdown": md, "summary_written": write_step_summary(md),
           "outputs_written": write_outputs({"decision": t["decision"], "exit-code": t["exit_code"],
                                             "rerun-tests": " ".join(t["rerun_tests"]),
                                             "rerun-names": " ".join(sorted({x.rsplit("::", 1)[-1] for x in t["rerun_tests"]}))})}
    if gh and pr:
        try:
            c = upsert_pr_comment(gh, pr, md, name)
            out["pr_comment"] = c.get("html_url") or c.get("id")
        except GitHubError as e:
            out["pr_comment_error"] = str(e)
    return out
