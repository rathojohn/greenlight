"""greenlight.toml: which repo to read, and from where.

Looked up in this order: --config, $GREENLIGHT_CONFIG, greenlight.toml in this folder or any parent,
~/.greenlight/config.toml. Every key is optional. Paths are relative to the file. Tokens never go
here: see github.resolve_token().
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE_NAME = "greenlight.toml"
_REMOTE = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")

DEFAULTS: dict[str, dict[str, Any]] = {
    "github": {"repo": None, "api_url": None},
    "git": {"path": None},
    "playtest": {"enabled": None, "runs_dir": "tools/playtest/runs", "known_failures": "tools/playtest/known-failures.json",
                 "report": "tools/playtest/out/report.json"},
    "actions": {"enabled": True, "junit_artifacts": "junit*", "days": 30},
    "delivery": {"deploy_branch": None, "deploy_environment": None, "deploy_releases": False,
                 "deploy_files": None, "deploy_files_ref": None, "incident_labels": ["incident", "hotfix", "bug"]},
    "issues": {"flaky_label": "flaky-test", "perf_label": "perf-regression", "quarantine_label": "quarantined",
               "min_flips": 2, "window_days": 30, "healed_runs": 10, "healed_days": 14},
    "ci": {"data_branch": "greenlight-data"},
    "sync": {"days": 90},
}


@dataclass
class Config:
    path: Path | None = None
    db: str | None = None
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, section: str, key: str) -> Any:
        return self.sections.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key))

    @property
    def base(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve_path(self, value: str | None) -> str | None:
        if not value:
            return None
        p = Path(os.path.expanduser(value))
        return str(p if p.is_absolute() else (self.base / p).resolve())

    @property
    def git_path(self) -> str | None:
        """The local clone: [git] path, else the folder the config sits in, if it is a git checkout."""
        explicit = self.resolve_path(self.get("git", "path"))
        if explicit:
            return explicit
        return str(self.base) if (self.base / ".git").exists() else None

    @property
    def repo(self) -> str | None:
        """owner/name: [github] repo, else parsed from the clone's origin remote."""
        repo = self.get("github", "repo") or os.environ.get("GITHUB_REPOSITORY")
        if repo:
            return repo
        return remote_repo(self.git_path) if self.git_path else None

    @property
    def api_url(self) -> str:
        return (self.get("github", "api_url") or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    @property
    def playtest_enabled(self) -> bool:
        flag = self.get("playtest", "enabled")
        if flag is not None:
            return bool(flag)
        git = self.git_path
        return bool(git and (Path(git) / self.get("playtest", "runs_dir")).is_dir())


def remote_repo(git_path: str) -> str | None:
    try:
        url = subprocess.run(["git", "-C", git_path, "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = _REMOTE.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def find(start: Path | None = None) -> Path | None:
    env = os.environ.get("GREENLIGHT_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / FILE_NAME).is_file():
            return d / FILE_NAME
    home = Path.home() / ".greenlight" / "config.toml"
    return home if home.is_file() else None


def load(path: str | None = None) -> Config:
    p = Path(os.path.expanduser(path)) if path else find()
    if p is None:
        return Config()
    if not p.is_file():
        raise FileNotFoundError(f"No config file at {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{p}: {e}") from e
    unknown = sorted(set(k for k, v in data.items() if isinstance(v, dict)) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"{p}: unknown section(s) {unknown}. Known: {sorted(DEFAULTS)}")
    cfg = Config(path=p.resolve(), sections={k: v for k, v in data.items() if isinstance(v, dict)})
    if data.get("db"):
        cfg.db = cfg.resolve_path(str(data["db"]))
    return cfg
