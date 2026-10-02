"""`flakewatch sync`: bring the local DB up to date from git and GitHub, per flakewatch.toml.
Each source runs on its own, so one failing (no token, rate limit) doesn't stop the rest."""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from . import ghsync, playtest
from .config import Config
from .github import GitHub, GitHubError, resolve_token
from .gitrepo import Repo

SOURCES = ("playtest", "pulls", "issues", "actions", "deployments")


def plan(cfg: Config, only: set[str] | None = None) -> list[str]:
    want = set(only or SOURCES)
    out = []
    if "playtest" in want and cfg.playtest_enabled:
        out.append("playtest")
    if cfg.repo:
        out += [s for s in ("pulls", "issues") if s in want]
        if "actions" in want and cfg.get("actions", "enabled"):
            out.append("actions")
    d = cfg.sections.get("delivery", {})
    if "deployments" in want and any(d.get(k) for k in ("deploy_branch", "deploy_environment", "deploy_releases",
                                                         "deploy_files")):
        out.append("deployments")
    return out


def run(conn: sqlite3.Connection, cfg: Config, only: set[str] | None = None,
        log: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    sources = plan(cfg, only)
    if not sources:
        raise ValueError("Nothing to sync. Add a flakewatch.toml with [github] repo (or run inside a GitHub clone), "
                         "or set [playtest] for a playtest ledger. `flakewatch init` writes one.")
    token, token_from = resolve_token()
    gh = GitHub(cfg.repo, token, cfg.api_url) if cfg.repo else None
    git = Repo(cfg.git_path) if cfg.git_path and Repo(cfg.git_path).ok() else None
    days = int(cfg.get("sync", "days"))
    report: dict[str, Any] = {"repo": cfg.repo, "token": token_from, "sources": {}, "errors": {}}
    for source in sources:
        log(f"syncing {source}")
        try:
            if source == "playtest":
                if not cfg.git_path:
                    raise ValueError("[playtest] needs a local clone: set [git] path")
                res = playtest.sync(conn, cfg.git_path, cfg.get("playtest", "runs_dir"),
                                    cfg.get("playtest", "known_failures"),
                                    cfg.resolve_path(cfg.sections.get("playtest", {}).get("report")))
            elif source == "pulls":
                res = ghsync.sync_pulls(conn, gh, days)
            elif source == "issues":
                res = ghsync.sync_issues(conn, gh, cfg.get("issues", "quarantine_label"), days)
            elif source == "actions":
                res = ghsync.sync_actions(conn, gh, int(cfg.get("actions", "days")), cfg.get("actions", "junit_artifacts"))
            else:
                delivery = {k: cfg.get("delivery", k) for k in
                            ("deploy_branch", "deploy_environment", "deploy_releases", "deploy_files", "deploy_files_ref")}
                res = ghsync.sync_deployments(conn, gh, git, delivery)
            report["sources"][source] = res
        except (GitHubError, ValueError, LookupError, OSError, RuntimeError) as e:
            report["errors"][source] = str(e)
    if gh:
        report["api_calls"] = gh.calls
    return report
