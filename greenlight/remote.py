"""Work from a GitHub repo name alone, with no clone of yours.

greenlight keeps its own cache clone per repo under ~/.greenlight/repos/<owner>/<name>: no files
checked out, and file contents fetched only when something reads them (--filter=blob:none), so it
stays small even for a big repo. `sync` then reads it like any clone: run records on every branch,
the data branch, release notes. A greenlight.toml committed on the default branch is honored.

Used by `greenlight mcp --repo` / `greenlight serve --repo` (and GREENLIGHT_REPO), which is how
Claude Desktop, claude.ai and ChatGPT connectors, and the container image get their data.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import config
from .github import resolve_token
from .gitrepo import GitError, Repo, run_git, set_auth

_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def home() -> Path:
    return Path(os.path.expanduser(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight"))


def check(repo: str) -> str:
    repo = repo.strip().removesuffix(".git")
    if repo.startswith(("https://github.com/", "http://github.com/")):
        repo = repo.split("github.com/", 1)[1].strip("/")
    if not _NAME.match(repo) or ".." in repo:
        raise ValueError(f"{repo!r} isn't a GitHub repo name. Use owner/name, like rathojohn/greenlight.")
    return repo


def clone_path(repo: str) -> Path:
    owner, name = check(repo).split("/")
    return home() / "repos" / owner / name


def db_path(repo: str) -> str:
    owner, name = check(repo).split("/")
    return str(home() / f"{owner}-{name}.db")


def git_base() -> str:
    """Where clones come from. GREENLIGHT_GIT_BASE points at GitHub Enterprise (or a folder, in tests)."""
    return (os.environ.get("GREENLIGHT_GIT_BASE") or "https://github.com").rstrip("/")


def ensure_clone(repo: str) -> Repo:
    """Clone on first use, fetch every branch after that."""
    repo = check(repo)
    base = git_base()
    token, _ = resolve_token() if base.startswith("https://") else (None, "")
    set_auth(token, base + "/")
    path = clone_path(repo)
    fresh = not (path / ".git").is_dir()
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = run_git(["clone", "--quiet", "--filter=blob:none", "--no-checkout", f"{base}/{repo}.git", str(path)],
                      capture_output=True)
    else:
        out = run_git(["-C", str(path), "fetch", "--quiet", "--prune", "origin"], capture_output=True)
    if out.returncode != 0:
        lines = out.stderr.decode(errors="replace").strip().splitlines()
        hint = "" if token else " If it's private, greenlight needs a token: see `greenlight auth`."
        raise GitError(f"couldn't {'clone' if fresh else 'fetch'} {repo}: {lines[-1] if lines else out.returncode}.{hint}")
    return Repo(str(path))


def config_for(repo: str) -> config.Config:
    """The config for a cache clone: the repo's committed greenlight.toml if it has one, pointed at the
    clone. Playtest ledgers are detected from the default branch, since the clone has no files."""
    repo = check(repo)
    r = ensure_clone(repo)
    default = r.default_branch()
    text = r.show(default, config.FILE_NAME)
    clone = Path(r.path)
    cfg = config.parse(text, f"{repo}:{config.FILE_NAME}", start=clone) if text else config.Config(start=clone)
    cfg.sections.setdefault("github", {})["repo"] = repo
    cfg.sections.setdefault("git", {})["path"] = str(clone)
    playtest = cfg.sections.setdefault("playtest", {})
    if playtest.get("enabled") is None:
        playtest["enabled"] = bool(r.ls_dir(default, cfg.get("playtest", "runs_dir")))
    if not text:
        guess_delivery(cfg, r, default)
    playtest.pop("report", None)  # a report.json only exists in someone's checkout
    cfg.db = db_path(repo)  # a committed db path names someone else's disk
    return cfg


def guess_delivery(cfg: config.Config, r: Repo, ref: str) -> None:
    """With no greenlight.toml, guess what marks a release the way `greenlight init` does: release notes,
    then a release branch, then GitHub Releases."""
    delivery = cfg.sections.setdefault("delivery", {})
    if any(delivery.get(k) for k in ("deploy_files", "deploy_branch", "deploy_environment", "deploy_releases")):
        return
    if r.ls_dir(ref, "docs/patch-notes"):
        delivery["deploy_files"] = "docs/patch-notes/20??-??-??-*.md"
    elif not cfg.repo:  # the other two come from the GitHub API
        return
    elif r.resolve("refs/remotes/origin/release") or r.resolve("refs/heads/release"):
        delivery["deploy_branch"] = "release"
    else:
        delivery["deploy_releases"] = True


class Refresher:
    """Keeps a repo's DB fresh for a long-running server: sync on first use, then at most once per
    `every` seconds, in the background, so a tool call never waits on GitHub after the first sync."""

    def __init__(self, sync: Callable[[], None], every: float = 600, ready: bool = False):
        self._sync = sync
        self.every = every
        self.last = 0.0
        self.error: str | None = None
        self.synced_at: float | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        if ready:  # data from an earlier run is on disk: serve it while the first refresh runs
            self._ready.set()

    def kick(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self.last and time.monotonic() - self.last < self.every:
                return
            self.last = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="greenlight-sync", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            self._sync()
            self.error = None
            self.synced_at = time.time()
        except Exception as e:  # noqa: BLE001 - reported to the caller on the next tool call
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def now(self) -> None:
        """Sync in the caller's thread (greenlight_sync), waiting out a background sync first."""
        thread = self._thread
        if thread and thread.is_alive():
            thread.join()
        with self._lock:
            self.last = time.monotonic()
        self._run()
        if self.error:
            raise RuntimeError(self.error)
