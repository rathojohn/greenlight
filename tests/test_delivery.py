import json
from datetime import timedelta

from greenlight import delivery
from greenlight.db import iso, utcnow


def at(hours_ago: float) -> str:
    return iso(utcnow() - timedelta(hours=hours_ago))


def add_pipeline(conn, run, attempt, status, sha, hours_ago, jobs=(("test", None),), duration=60000):
    pid = f"gha:{run}:{attempt}"
    conn.execute("INSERT INTO pipelines (pipeline_id, provider, workflow, run_number, attempt, branch, commit_sha, status, "
                 "created_at, started_at, finished_at, duration_ms, queue_ms) VALUES (?, 'github-actions', 'CI', ?, ?, "
                 "'main', ?, ?, ?, ?, ?, ?, 5000)",
                 (pid, run, attempt, sha, status, at(hours_ago), at(hours_ago), at(hours_ago - .1), duration))
    for i, (name, jstatus) in enumerate(jobs):
        conn.execute("INSERT INTO jobs (job_id, pipeline_id, name, status, started_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
                     (f"gha:{run}{attempt}{i}", pid, name, jstatus or status, at(hours_ago), duration))


def test_pipeline_summary_and_flaky_jobs(db):
    conn, _ = db
    add_pipeline(conn, 1, 1, "failure", "s1", 30, jobs=[("test", "failure"), ("lint", "success")])
    add_pipeline(conn, 1, 2, "success", "s1", 29, jobs=[("test", "success")])
    add_pipeline(conn, 2, 1, "success", "s2", 10, duration=120000)
    add_pipeline(conn, 3, 1, "failure", "s3", 5)
    s = delivery.pipelines_summary(conn, 7)
    assert s["totals"] == {"runs": 3, "failed": 1, "rerun": 1, "minutes": 5.0}
    wf = s["workflows"][0]
    assert (wf["runs"], wf["attempts"], wf["rerun_runs"]) == (3, 4, 1)
    assert wf["success_rate"] == round(2 / 3, 4)
    assert wf["p50_duration_ms"] == 60000 and wf["p95_duration_ms"] == 120000
    assert [(j["name"], j["flips"], j["eligible"]) for j in s["flaky_jobs"]] == [("test", 1, 1)]
    assert sum(s["success_per_day"]) == 2 and sum(s["failed_per_day"]) == 1
    detail = delivery.pipeline_detail(conn, "gha:1:2")
    assert [a["attempt"] for a in detail["attempts"]] == [1, 2] and detail["jobs"][0]["name"] == "test"


def add_deploy(conn, did, sha, hours_ago, status="success", env="production", commits=()):
    conn.execute("INSERT INTO deployments (deploy_id, source, environment, commit_sha, deployed_at, status) "
                 "VALUES (?, 'git-file', ?, ?, ?, ?)", (did, env, sha, at(hours_ago), status))
    for c_sha, authored_hours_ago in commits:
        conn.execute("INSERT INTO deploy_commits VALUES (?, ?, ?)", (did, c_sha, at(authored_hours_ago)))


def add_issue(conn, n, labels, created_hours_ago, closed_hours_ago=None, key=None):
    conn.execute("INSERT INTO issues (number, title, state, labels, created_at, closed_at, managed_key) VALUES (?,?,?,?,?,?,?)",
                 (n, f"i{n}", "closed" if closed_hours_ago is not None else "open", json.dumps(labels),
                  at(created_hours_ago), at(closed_hours_ago) if closed_hours_ago is not None else None, key))


def test_dora_metrics(db):
    conn, _ = db
    add_deploy(conn, "d1", "a", 72, commits=[("c1", 80), ("a", 74)])        # lead 8h, 2h
    add_deploy(conn, "d2", "b", 48, commits=[("c2", 52)])                   # lead 4h
    add_deploy(conn, "d3", "c", 24, status="failure", commits=[("c3", 30)])
    add_deploy(conn, "d4", "d", 2, commits=[("c4", 12)])                    # lead 10h
    add_deploy(conn, "old", "z", 24 * 60)                                   # outside the window
    add_deploy(conn, "s1", "s", 5, env="staging")
    add_issue(conn, 1, ["bug"], 40, closed_hours_ago=36)    # after d2: d2 failed, restored in 4h
    add_issue(conn, 2, ["question"], 30)                    # not an incident
    d = delivery.dora(conn, 7)
    m = d["metrics"]
    assert d["environment"] == "production"
    assert m["deployments"] == 3 and m["failed_deployments"] == 2
    assert m["change_failure_rate"] == round(2 / 4, 4)
    assert m["lead_time_p50_h"] is not None and 3.9 < m["lead_time_p50_h"] < 6.1
    assert 3.9 < m["time_to_restore_p50_h"] < 4.1
    assert m["incidents"] == 1 and m["open_incidents"] == 0
    by_id = {r["deploy_id"]: r for r in d["deployments"]}
    assert by_id["d2"]["incidents"] == [1] and by_id["d1"]["failed"] is False
    assert by_id["d1"]["commits"] == 2
    assert {e["environment"] for e in d["environments"]} == {"production", "staging"}
    assert delivery.dora(conn, 7, environment="staging")["metrics"]["deployments"] == 1


def test_pr_flow_and_issues(db):
    conn, _ = db
    conn.execute("INSERT INTO pull_requests (number, state, created_at, merged_at) VALUES (1, 'merged', ?, ?)", (at(10), at(8)))
    conn.execute("INSERT INTO pull_requests (number, state, created_at, merged_at) VALUES (2, 'merged', ?, ?)", (at(10), at(6)))
    conn.execute("INSERT INTO pull_requests (number, state, created_at) VALUES (3, 'open', ?)", (at(50),))
    p = delivery.pr_flow(conn, 7)
    assert p["metrics"]["merged"] == 2 and p["metrics"]["open"] == 1
    assert 1.9 < p["metrics"]["time_to_merge_p50_h"] < 2.1
    assert p["open"][0]["number"] == 3
    add_issue(conn, 5, ["flaky-test"], 20, key="flaky:smoke::x")
    add_issue(conn, 6, ["bug"], 100, closed_hours_ago=1)
    i = delivery.issues_summary(conn, 7)
    assert i["metrics"]["open"] == 1 and i["metrics"]["closed"] == 1
    assert i["managed"][0]["test_id"] == "smoke::x" and i["managed"][0]["kind"] == "flaky"
    assert i["open_by_label"] == [("flaky-test", 1)]


def test_percentile():
    assert delivery.pct([], .5) is None
    assert delivery.pct([5, 1, 3], .5) == 3
    assert delivery.pct([1, 2, 3, 4], .95) == 4
