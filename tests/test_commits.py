"""Commits and merges, and the Claude Code conversations they came from: what the usage hook finds in a transcript
and the checkout's reflog, and how the server joins it to runs, CI, pull requests and deployments."""
import json
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from greenlight import commits, context, dashboard, usage, web
from greenlight.db import connect, iso
from greenlight.ingest import TestResult as Result, record_run

from tests.conftest import child_env

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=1)


def at(minutes: float) -> str:
    return iso(T0 + timedelta(minutes=minutes))


def z(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def call(minutes: float, done: float, tool_id: str, name: str, args: dict, result, branch: str = "fix-login",
         error: bool = False) -> list[str]:
    """A tool call and its result, as Claude Code writes them."""
    return [json.dumps({"type": "assistant", "timestamp": z(minutes), "gitBranch": branch, "sessionId": "s1",
                        "message": {"id": f"m-{tool_id}", "model": "claude-opus-5-5",
                                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
                                    "usage": {"input_tokens": 1, "output_tokens": 10}}}),
            json.dumps({"type": "user", "timestamp": z(done), "gitBranch": branch, "sessionId": "s1",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": result,
                                                 **({"is_error": True} if error else {})}]}})]


def git(repo, *args: str, when: str | None = None) -> str:
    env = child_env(**({"GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when} if when else {}))
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


def test_a_transcript_says_when_it_committed_and_what_it_merged(tmp_path):
    merged = json.dumps({"sha": "a" * 40, "merged": True, "message": "Pull Request successfully merged"})
    lines = [
        *call(0, 0.05, "t1", "Bash", {"command": "cd repo && git -c user.name=x add -A && git -C . commit -q -m 'fix: it'"}, ""),
        *call(1, 1.01, "t2", "Bash", {"command": "git commit-tree HEAD^{tree} -m x"}, "sha"),  # plumbing, not a commit
        *call(2, 2.02, "t3", "Bash", {"command": "git status"}, "clean"),
        *call(3, 3.02, "t4", "Bash", {"command": "gh pr merge 12 --squash --delete-branch"}, "Merged"),
        *call(4, 4.02, "t5", "Bash", {"command": "gh pr merge https://github.com/o/r/pull/13 --auto"}, "ok"),
        *call(5, 5.02, "t6", "mcp__github__merge_pull_request", {"owner": "o", "repo": "r", "pullNumber": 14},
              [{"type": "text", "text": merged}]),
        *call(6, 6.02, "t7", "mcp__github__merge_pull_request", {"pullNumber": 15},
              json.dumps({"message": "not mergeable"})),  # no sha, didn't merge
        *call(7, 7.02, "t8", "Bash", {"command": "git commit -m broken"}, "hook failed", error=True),
    ]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(lines) + "\n")
    acts = context.read_file(path)["actions"]
    assert [(a["kind"], a.get("pr"), a.get("sha")) for a in acts] == [
        ("commit", None, None), ("merge", 12, ""), ("merge", 13, ""), ("merge", 14, "a" * 40)]
    assert acts[0]["from"] == at(0) and acts[0]["to"] == at(0.05)[:19] + "+00:00" and acts[0]["branch"] == "fix-login"


def test_the_reflog_names_the_exact_commit_made_during_a_git_command(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "before the session", when=at(-30))
    git(repo, "checkout", "-q", "-b", "fix-login")
    git(repo, "commit", "-q", "--allow-empty", "-m", "the session's", when=at(10))
    mine = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "by hand, later", when=at(20))
    found = usage.reflog(str(repo))
    assert {c["sha"] for c in found if c["branch"] == "fix-login"} >= {mine}
    actions = [{"kind": "commit", "from": at(9.99), "to": at(10.01), "branch": "fix-login"},
               {"kind": "merge", "at": at(15), "sha": "", "pr": 3, "branch": "fix-login"}]
    assert usage.commits(actions, str(repo)) == [
        {"kind": "merge", "at": at(15), "sha": "", "pr": 3, "branch": "fix-login"},
        {"kind": "commit", "at": at(10), "sha": mine, "pr": None, "branch": "fix-login"}]
    assert usage.commits(actions, str(tmp_path / "not-a-repo"))[1:] == []  # no reflog: only the merge


def test_a_title_is_the_first_prompt_with_keys_and_paths_left_out(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "out"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": '\n  @"/home/me/My Files/report.zip" fix the login '
                                                  'loop, token ghp_abcdefghijklmnopqrstuvwxyz0123\nsecond line'}]}},
    ]) + "\n")
    assert usage.first_prompt(path) == "@report.zip fix the login loop, token [redacted]"
    path.write_text(json.dumps({"type": "user", "message": {
        "content": "<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>12</command-args>"}}) + "\n")
    assert usage.first_prompt(path) == "/review 12"
    long = "word " * 40
    assert usage.title(long).endswith("...") and len(usage.title(long)) <= usage.TITLE_CHARS + 3
    assert usage.title("see https://github.com/rathojohn/greenlight/pull/20 please") == \
        "see https://github.com/rathojohn/greenlight/pull/20 please"  # a URL isn't a key
    assert usage.first_prompt(tmp_path / "missing.jsonl") is None


def test_the_server_keeps_a_sessions_commits_and_title_and_clears_a_title_turned_off(tmp_path):
    with closing(connect(str(tmp_path / "u.db"))) as conn:
        made = [{"kind": "commit", "at": at(1), "sha": "B" * 40, "branch": "fix"},
                {"kind": "merge", "at": at(2), "sha": "", "pr": "7", "branch": "fix"},
                {"kind": "commit", "at": at(3), "sha": "not hex"}, {"kind": "push", "at": at(4), "sha": "c" * 40}]
        usage.store(conn, {"session": {"session_id": "s", "title": "Fix the login loop"}, "rows": [], "commits": made})
        assert [tuple(r) for r in conn.execute("SELECT kind, sha, pr FROM agent_commits ORDER BY at")] == [
            ("commit", "b" * 40, None), ("merge", "", 7)]
        assert conn.execute("SELECT title FROM agent_sessions").fetchone()[0] == "Fix the login loop"
        usage.store(conn, {"session": {"session_id": "s"}, "rows": []})  # an older client: both kept
        assert conn.execute("SELECT COUNT(*) FROM agent_commits").fetchone()[0] == 2
        assert conn.execute("SELECT title FROM agent_sessions").fetchone()[0] == "Fix the login loop"
        usage.store(conn, {"session": {"session_id": "s", "title": None}, "rows": [], "commits": []})
        assert conn.execute("SELECT title FROM agent_sessions").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM agent_commits").fetchone()[0] == 0


def test_the_hook_sends_no_title_when_titles_are_off(tmp_path):
    path = tmp_path / "s1.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "fix the login loop"}}) + "\n")
    hook = {"transcript_path": str(path), "session_id": "s1", "cwd": str(tmp_path)}
    assert usage.payload_from_hook(hook, {})["session"]["title"] == "fix the login loop"
    (tmp_path / "greenlight.toml").write_text("[usage]\ntitles = false\n")
    assert usage.payload_from_hook(hook, {})["session"]["title"] is None


A, B, M, H = "a" * 40, "b" * 40, "c" * 40, "d" * 40  # a session's commit, a ledger's, a squash merge, by hand


@pytest.fixture
def history(tmp_path):
    """Two sessions: s1 made A on fix-login and merged its pull request as M; s2 ran the tests on A and on B (by its
    short sha, like a test ledger). CI ran on A and M; a release shipped M. H was pushed by hand with CI only."""
    conn = connect(str(tmp_path / "c.db"))
    conn.execute("INSERT INTO pull_requests (number, title, state, base, head, head_sha, merge_sha, created_at, merged_at, url) "
                 "VALUES (7, 'Fix the login loop', 'merged', 'main', 'fix-login', ?, ?, ?, ?, "
                 "'https://github.com/o/r/pull/7')", (A, M, at(0), at(30)))
    usage.store(conn, {"session": {"session_id": "s1", "remote_session": "session_01X", "title": "Fix the login loop"},
                       "rows": [{"minute": at(m), "model": "m", "branch": "fix-login", "requests": 1, "output_tokens": 5}
                                for m in range(0, 30)],
                       "commits": [{"kind": "commit", "at": at(10), "sha": A, "branch": "fix-login"},
                                   {"kind": "merge", "at": at(30), "sha": M, "pr": 7, "branch": "fix-login"}]})
    usage.store(conn, {"session": {"session_id": "s2", "title": "Check the ledger"}, "rows": [
        {"minute": at(40), "model": "m", "branch": "main", "requests": 1, "output_tokens": 1}]})
    record_run(conn, [Result("t::login", None, "fail", 5)], commit_sha=A, branch="fix-login", started_at=T0 + timedelta(minutes=12),
               session="s2", source="local")
    record_run(conn, [Result("t::login", None, "pass", 5)], commit_sha=B[:7] + "+edits", git_commit=B[:7], branch="main",
               started_at=T0 + timedelta(minutes=41), session="s2", source="playtest")
    record_run(conn, [Result("t::login", None, "pass", 5)], commit_sha=B, branch="main",
               started_at=T0 + timedelta(minutes=42), session="ci-123", source="ci")
    for pid, sha, status, attempt, minute in (("gha:1:1", A, "failure", 1, 11), ("gha:1:2", A, "success", 2, 13),
                                              ("gha:2:1", M, "success", 1, 31), ("gha:3:1", H, "success", 1, 50)):
        conn.execute("INSERT INTO pipelines (pipeline_id, provider, workflow, attempt, branch, commit_sha, status, created_at, url) "
                     "VALUES (?, 'gha', 'CI', ?, 'main', ?, ?, ?, 'https://github.com/o/r/actions/runs/1')",
                     (pid, attempt, sha, status, at(minute)))
    conn.execute("INSERT INTO deployments VALUES ('d1', 'branch-push', 'production', ?, NULL, ?, 'success', 'v1', NULL)",
                 (M, at(35)))
    conn.execute("INSERT INTO deploy_commits VALUES ('d1', ?, ?)", (M, at(30)))
    conn.commit()
    yield conn
    conn.close()


def test_commits_join_what_made_merged_tested_and_shipped_them(history):
    out = commits.commit_list(history, 3)
    rows = {c["sha"]: c for c in out["commits"]}
    assert set(rows) == {A, B, M, H}  # the ledger's short sha joined the full one
    a = rows[A]
    assert a["pr"]["number"] == 7 and a["branch"] == "fix-login" and a["at"] == at(10)
    assert [(c["session_id"], c["how"]) for c in a["conversations"]] == [("s1", "made"), ("s2", "tested")]
    assert a["conversations"][0]["url"] == "https://claude.ai/code/session_01X"
    assert a["ci"] == "success"  # the latest attempt passed
    assert a["decision"] == "REAL_FAILURE" and a["runs"] == 1
    m = rows[M]
    assert m["kind"] == "merge" and m["branch"] == "main" and m["merged_by"]["session_id"] == "s1"
    assert m["deploys"][0]["environment"] == "production"
    assert rows[B]["runs"] == 2 and [c["session_id"] for c in rows[B]["conversations"]] == ["s2"]
    assert rows[H]["conversations"] == [] and rows[H]["ci"] == "success"
    assert out["repo_url"] == "https://github.com/o/r"
    merge = out["merges"][0]
    assert merge["number"] == 7 and merge["commits"] == 1 and merge["merged_by"]["session_id"] == "s1"
    assert [(c["session_id"], c["how"]) for c in merge["conversations"]] == [("s1", "made"), ("s2", "tested")]


def test_one_commit_has_its_runs_ci_and_conversations(history):
    d = commits.commit_detail(history, B[:7])
    assert d["sha"] == B and [r["source"] for r in d["test_runs"]] == ["ci", "playtest"]
    assert d["test_runs"][1]["conversation"]["session_id"] == "s2" and d["test_runs"][0]["conversation"] is None
    a = commits.commit_detail(history, A)
    assert [r["status"] for r in a["ci_runs"]] == ["success", "failure"]
    with pytest.raises(LookupError):
        commits.commit_detail(history, "e" * 40)
    with pytest.raises(ValueError):
        commits.commit_detail(history, "main")


def test_runs_ci_and_panels_name_the_conversation(history):
    runs = {r["source"]: r for r in dashboard.runs_list(history, 10)["runs"]}
    assert runs["local"]["conversation"]["how"] == "tested"  # it ran them
    assert runs["ci"]["conversation"] is None  # B came from no recorded session
    by_sha = {r["commit_sha"]: r["conversation"] for r in web._pipelines(history, 3)["recent"]}
    assert by_sha[A]["session_id"] == "s1" and by_sha[H] is None
    pr = usage.detail(history, 3, "pr", "7")
    assert pr["merged_by"]["session_id"] == "s1" and {c["sha"] for c in pr["commits"]} == {A, M}
    s1 = usage.detail(history, 3, "session", "session_01X")
    assert s1["session"]["title"] == "Fix the login loop"
    assert [(c["kind"], c["sha"], c["pr"]["number"]) for c in s1["commits"]] == [("merge", M, 7), ("commit", A, 7)]


def test_a_merge_known_only_by_its_number_finds_its_commit(history):
    usage.store(history, {"session": {"session_id": "s3"}, "rows": [],
                          "commits": [{"kind": "merge", "at": at(30), "sha": "", "pr": 7, "branch": "fix-login"}]})
    m = next(c for c in commits.commit_list(history, 3)["commits"] if c["sha"] == M)
    assert {c["session_id"] for c in m["conversations"]} == {"s1", "s3"}
    assert commits.for_session(history, "s3")[0]["sha"] == M
