import io
import json
import os
import subprocess
import zipfile

import pytest

from greenlight import ghsync, github
from greenlight.gitrepo import Repo
from tests.conftest import junit_xml
from tests.fakegithub import FakeGitHub


@pytest.fixture
def fake():
    f = FakeGitHub("o/r")
    yield f
    f.close()


@pytest.fixture
def gh(fake):
    return github.GitHub("o/r", "secret-token", fake.url, retries=1)


# ---------- auth ----------
def test_token_precedence(monkeypatch, tmp_path):
    for var in github.TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no gh on PATH
    monkeypatch.setattr(github, "_GH_FALLBACKS", [])
    assert github.resolve_token() == (None, "none")
    monkeypatch.setenv("GITHUB_TOKEN", "a")
    monkeypatch.setenv("GH_TOKEN", "b")
    assert github.resolve_token() == ("b", "$GH_TOKEN")
    monkeypatch.setenv("GREENLIGHT_GITHUB_TOKEN", "c")
    assert github.resolve_token() == ("c", "$GREENLIGHT_GITHUB_TOKEN")


def test_token_from_gh_cli(monkeypatch, tmp_path):
    for var in github.TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)
    if os.name == "nt":
        (tmp_path / "gh.bat").write_text('@echo off\r\nif "%1 %2"=="auth token" echo from-gh\r\n')
    else:
        gh_bin = tmp_path / "gh"
        gh_bin.write_text("#!/bin/sh\n[ \"$1 $2\" = \"auth token\" ] && echo from-gh\n")
        gh_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(github, "_GH_FALLBACKS", [])
    assert github.resolve_token() == ("from-gh", "gh auth token")


def test_repo_name_is_validated():
    with pytest.raises(ValueError):
        github.GitHub("not a repo")


# ---------- client ----------
def test_sends_token_and_follows_pages(fake, gh):
    fake.pages("GET", "{repo}/pulls", [{"n": i} for i in range(5)], per_page=2)
    assert [x["n"] for x in gh.paginate("/pulls")] == [0, 1, 2, 3, 4]
    reqs = fake.calls("GET", "{repo}/pulls")
    assert len(reqs) == 3 and reqs[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert reqs[0]["query"]["per_page"] == "100"


def test_errors_are_actionable(fake, gh):
    fake.route("GET", "{repo}/a", lambda q, b: (401, {"message": "Bad credentials"}))
    fake.route("GET", "{repo}/b", lambda q, b: (403, {"message": "API rate limit exceeded"},
                                                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}))
    fake.route("GET", "{repo}/c", lambda q, b: (403, {"message": "Resource not accessible by integration"}))
    with pytest.raises(github.GitHubError, match="rejected the token"):
        gh.get("/a")
    with pytest.raises(github.RateLimited, match="resets at"):
        gh.get("/b")
    with pytest.raises(github.GitHubError, match="lack a permission"):
        gh.get("/c")
    with pytest.raises(github.GitHubError, match="404"):
        gh.get("/missing")


def test_retries_a_502(fake, gh):
    calls = []
    fake.route("GET", "{repo}/flaky", lambda q, b: (calls.append(1), (502, {}) if len(calls) == 1 else (200, {"ok": 1}))[1])
    assert gh.get("/flaky") == {"ok": 1} and len(calls) == 2


def test_download_does_not_send_the_token_to_the_redirect(fake, gh):
    fake.route("GET", "{repo}/actions/artifacts/9/zip",
               lambda q, b: (302, {}, {"Location": f"{fake.url}/blob/9"}))
    fake.route("GET", "/blob/9", lambda q, b: (200, b"zipbytes"))
    assert gh.download("/actions/artifacts/9/zip") == b"zipbytes"
    blob = fake.calls("GET", "/blob/9")[0]
    assert "Authorization" not in blob["headers"]
    assert fake.calls("GET", "{repo}/actions/artifacts")[0]["headers"]["Authorization"] == "Bearer secret-token"


# ---------- sync: pull requests and issues ----------
def pr(n, state="closed", merged=True, updated="2026-10-01T10:00:00Z"):
    return {"number": n, "title": f"PR {n}", "user": {"login": "me"}, "state": state, "draft": False,
            "base": {"ref": "main"}, "head": {"ref": f"claude/b{n}", "sha": f"h{n}"},
            "merge_commit_sha": f"m{n}", "created_at": "2026-10-01T09:00:00Z",
            "merged_at": "2026-10-01T09:30:00Z" if merged else None,
            "closed_at": "2026-10-01T09:30:00Z" if state == "closed" else None,
            "updated_at": updated, "html_url": f"https://github.com/o/r/pull/{n}"}


def test_sync_pulls_stops_at_the_high_water_mark(db, fake, gh):
    conn, _ = db
    fake.pages("GET", "{repo}/pulls", [pr(3, "open", False, "2099-01-03T00:00:00Z"),
                                       pr(2, updated="2099-01-02T00:00:00Z"),
                                       pr(1, "closed", False, "2000-01-01T00:00:00Z")])
    assert ghsync.sync_pulls(conn, gh) == {"pull_requests": 2}
    states = dict(conn.execute("SELECT number, state FROM pull_requests").fetchall())
    assert states == {3: "open", 2: "merged"}
    assert ghsync.sync_pulls(conn, gh) == {"pull_requests": 1}  # only #3 is at the mark


def issue(n, body="", labels=(), state="open", pr_link=False):
    it = {"number": n, "title": f"Issue {n}", "state": state, "body": body, "labels": [{"name": x} for x in labels],
          "user": {"login": "me"}, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T01:00:00Z",
          "closed_at": None, "comments": 0, "html_url": f"https://github.com/o/r/issues/{n}"}
    if pr_link:
        it["pull_request"] = {"url": "x"}
    return it


def test_sync_issues_skips_prs_reads_markers_and_mirrors_quarantine_labels(db, fake, gh):
    conn, _ = db
    conn.execute("INSERT INTO quarantine VALUES ('manual::t', 'mine', '2026-01-01', 'manual')")
    fake.pages("GET", "{repo}/issues", [
        issue(1, "<!-- greenlight:flaky:smoke::the Lantern -->", ["flaky-test", "quarantined"]),
        issue(2, "<!-- greenlight:perf:perf::early -->", ["perf-regression"]),
        issue(3, pr_link=True),
        issue(4, "plain bug", ["bug"]),
    ])
    out = ghsync.sync_issues(conn, gh)
    assert out == {"issues": 3, "quarantined_by_label": 1}
    keys = dict(conn.execute("SELECT number, managed_key FROM issues").fetchall())
    assert keys == {1: "flaky:smoke::the Lantern", 2: "perf:perf::early", 4: None}
    q = {r["test_id"]: r["added_by"] for r in conn.execute("SELECT * FROM quarantine")}
    assert q == {"manual::t": "manual", "smoke::the Lantern": "github#1"}
    # the label comes off: the next sync releases it and leaves the manual one
    conn.execute("UPDATE issues SET labels = '[\"flaky-test\"]' WHERE number = 1")
    ghsync.mirror_issue_quarantine(conn, "quarantined")
    assert {r["test_id"] for r in conn.execute("SELECT * FROM quarantine")} == {"manual::t"}


# ---------- sync: Actions ----------
def wf_run(rid, attempt=1, conclusion="success", sha="abc"):
    return {"id": rid, "name": "CI", "run_number": rid, "run_attempt": attempt, "event": "push", "status": "completed",
            "conclusion": conclusion, "head_branch": "main", "head_sha": sha,
            "created_at": "2026-10-01T10:00:00Z", "run_started_at": "2026-10-01T10:20:00Z",
            "updated_at": "2026-10-01T10:25:00Z", "html_url": f"https://github.com/o/r/actions/runs/{rid}",
            "actor": {"login": "me"}}


def job(jid, attempt, conclusion, start="2026-10-01T10:01:00Z", end="2026-10-01T10:04:00Z"):
    return {"id": jid, "run_attempt": attempt, "name": "test (3.12)", "status": "completed", "conclusion": conclusion,
            "created_at": "2026-10-01T10:00:30Z", "started_at": start, "completed_at": end, "runner_name": "gh-1",
            "html_url": f"https://github.com/o/r/actions/runs/1/job/{jid}",
            "steps": [{"number": 1, "name": "pytest", "status": "completed", "conclusion": conclusion,
                       "started_at": start, "completed_at": end}]}


def zip_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_sync_actions_stores_every_attempt_and_ingests_junit(db, fake, gh):
    conn, _ = db
    fake.pages("GET", "{repo}/actions/runs", [wf_run(1, attempt=2)], key="workflow_runs")
    fake.pages("GET", "{repo}/actions/runs/1/jobs", [
        job(10, 1, "failure"), job(11, 2, "success", "2026-10-01T10:21:00Z", "2026-10-01T10:24:00Z")], key="jobs")
    fake.pages("GET", "{repo}/actions/runs/1/artifacts", [
        {"id": 5, "name": "junit-py3.12-attempt-1", "expired": False},
        {"id": 6, "name": "junit-py3.12-attempt-2", "expired": False},
        {"id": 7, "name": "coverage", "expired": False}], key="artifacts")
    fail = junit_xml([("t.a::x", "fail", 0.1, "boom")])
    ok = junit_xml([("t.a::x", "pass", 0.1, None)])
    for aid, body in ((5, fail), (6, ok)):
        fake.route("GET", f"{{repo}}/actions/artifacts/{aid}/zip",
                   lambda q, b, aid=aid: (302, {}, {"Location": f"{fake.url}/blob/{aid}"}))
        fake.route("GET", f"/blob/{aid}", lambda q, b, body=body: (200, zip_of({"reports/junit.xml": body})))

    out = ghsync.sync_actions(conn, gh, junit_glob="junit*")
    assert out["workflow_runs"] == 1 and out["pipelines_stored"] == 2 and out["junit_runs"] == 2
    rows = conn.execute("SELECT pipeline_id, attempt, status, duration_ms, queue_ms FROM pipelines ORDER BY attempt").fetchall()
    assert [tuple(r) for r in rows] == [("gha:1:1", 1, "failure", 180000, 60000), ("gha:1:2", 2, "success", 300000, 60000)]
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 2
    from greenlight import analysis
    assert analysis.flake_stats(conn, 3650)["t.a::x"]["flip_shas"] == 1  # failed on attempt 1, passed on 2
    assert not fake.calls("GET", "{repo}/actions/artifacts/7")
    # second sync: the run is finished and stored, so jobs aren't fetched again
    before = len(fake.calls("GET", "{repo}/actions/runs/1/jobs"))
    ghsync.sync_actions(conn, gh, junit_glob="junit*")
    assert len(fake.calls("GET", "{repo}/actions/runs/1/jobs")) == before


# ---------- sync: deployments ----------
def sh(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.email", "t@t")
    sh(repo, "config", "user.name", "t")
    shas = []
    for i, day in enumerate(["01", "02", "03", "04"]):
        (repo / f"f{i}").write_text(str(i))
        if i in (1, 3):
            notes = repo / "docs/patch-notes"
            notes.mkdir(parents=True, exist_ok=True)
            (notes / f"2026-10-{day}-build-{i}.md").write_text("notes")
        sh(repo, "add", "-A")
        when = f"2026-10-{day}T12:00:00+00:00"
        sh(repo, "commit", "-qm", f"c{i}", env={**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        shas.append(sh(repo, "rev-parse", "HEAD"))
    return repo, shas


def test_deploy_files_and_lead_time_commits_from_git(db, clone):
    conn, _ = db
    repo, shas = clone
    r = Repo(str(repo))
    assert ghsync.sync_deploy_files(conn, r, "docs/patch-notes/20??-??-??-*.md", "main") == 2
    ghsync.link_deploy_commits(conn, None, r)
    deploys = conn.execute("SELECT deploy_id, commit_sha, deployed_at, version FROM deployments ORDER BY deployed_at").fetchall()
    assert [(d["commit_sha"], d["version"]) for d in deploys] == [(shas[1], "2026-10-02-build-1"), (shas[3], "2026-10-04-build-3")]
    shipped = conn.execute("SELECT commit_sha FROM deploy_commits WHERE deploy_id = ? ORDER BY authored_at",
                           (deploys[1]["deploy_id"],)).fetchall()
    assert [s[0] for s in shipped] == [shas[2], shas[3]]
    first = conn.execute("SELECT commit_sha FROM deploy_commits WHERE deploy_id = ?", (deploys[0]["deploy_id"],)).fetchall()
    assert [s[0] for s in first] == [shas[1]]


def test_branch_pushes_from_the_activity_api(db, fake, gh):
    conn, _ = db
    fake.pages("GET", "{repo}/activity", [
        {"id": 3, "activity_type": "push", "before": "b2", "after": "a3", "timestamp": "2026-10-02T01:48:00Z"},
        {"id": 2, "activity_type": "branch_deletion", "before": "x", "after": "0" * 40, "timestamp": "2026-10-01T00:00:00Z"},
        {"id": 1, "activity_type": "branch_creation", "before": "0" * 40, "after": "a1", "timestamp": "2026-09-28T00:00:00Z"},
    ])
    assert ghsync.sync_branch_pushes(conn, gh, "release") == 2
    assert fake.calls("GET", "{repo}/activity")[0]["query"]["ref"] == "refs/heads/release"
    fake.route("GET", r"{repo}/compare/b2\.\.\.a3", {"commits": [
        {"sha": "c1", "commit": {"author": {"date": "2026-10-01T20:00:00Z"}}},
        {"sha": "a3", "commit": {"author": {"date": "2026-10-02T01:00:00Z"}}}]})
    fake.route("GET", "{repo}/commits/a1", {"sha": "a1", "commit": {"author": {"date": "2026-09-27T00:00:00Z"}}})
    assert ghsync.link_deploy_commits(conn, gh, None) == 3
    prev = dict(conn.execute("SELECT commit_sha, previous_sha FROM deployments").fetchall())
    assert prev == {"a3": "b2", "a1": None}


def test_github_deployments_and_releases(db, fake, gh):
    conn, _ = db
    fake.pages("GET", "{repo}/deployments", [
        {"id": 1, "sha": "s1", "ref": "main", "created_at": "2026-10-01T00:00:00Z"},
        {"id": 2, "sha": "s2", "ref": "main", "created_at": "2026-10-02T00:00:00Z"}])
    fake.route("GET", "{repo}/deployments/1/statuses", [{"state": "success", "created_at": "2026-10-01T00:05:00Z",
                                                        "environment_url": "https://x"}])
    fake.route("GET", "{repo}/deployments/2/statuses", [{"state": "failure", "created_at": "2026-10-02T00:05:00Z"}])
    assert ghsync.sync_gh_deployments(conn, gh, "github-pages") == 2
    fake.pages("GET", "{repo}/releases", [
        {"id": 7, "tag_name": "v1", "draft": False, "published_at": "2026-10-03T00:00:00Z", "html_url": "u"},
        {"id": 8, "tag_name": "v2", "draft": True, "published_at": None}])
    fake.route("GET", "{repo}/commits/v1", {"sha": "s7"})
    assert ghsync.sync_releases(conn, gh, None) == 1
    rows = {r["deploy_id"]: (r["status"], r["commit_sha"]) for r in conn.execute("SELECT * FROM deployments")}
    assert rows == {"gh-deployment:1": ("success", "s1"), "gh-deployment:2": ("failure", "s2"),
                    "gh-release:7": ("success", "s7")}
    assert json.dumps(rows)  # plain data
