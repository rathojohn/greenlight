"""Read-only helpers over a local git clone: refs, files on refs, and commit metadata."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def code_identity(commit: str, changed: dict[str, str | None]) -> str:
    """The commit, plus a short hash of the uncommitted files' contents when there were any. Two runs
    flip only when they tested the same code, so a run with local edits is its own version."""
    if not changed:
        return commit
    digest = hashlib.sha1(json.dumps(sorted(changed.items())).encode()).hexdigest()[:8]
    return f"{commit}+{digest}"


class GitError(RuntimeError):
    pass


@dataclass
class Repo:
    path: str

    def git(self, *args: str, check: bool = True, input: bytes | None = None) -> str:
        try:
            out = subprocess.run(["git", "-C", self.path, *args], capture_output=True, check=False, input=input)
        except FileNotFoundError as e:
            raise GitError("git is not installed or not on PATH") from e
        if check and out.returncode != 0:
            msg = out.stderr.decode(errors="replace").strip() or f"exit {out.returncode}"
            raise GitError(f"git {' '.join(args[:3])}: {msg}")
        return out.stdout.decode("utf-8", errors="replace")

    def dirty(self, exclude: set[str] | None = None) -> dict[str, str | None]:
        """{path from the repo root: content hash, or None if deleted} for every file that differs from
        HEAD, untracked ones included (ignored ones not). Mirrors survive-project's ledger.cjs.
        exclude: files to leave out (absolute, or from the repo root), like the report being recorded."""
        top = self.git("rev-parse", "--show-toplevel", check=False).strip()
        if not top:
            return {}
        root = Repo(top)
        skip = {os.path.relpath(e, top) if os.path.isabs(e) else e for e in (exclude or ())}
        names = set(filter(None, root.git("diff", "--name-only", "-z", "HEAD", check=False).split("\0")))
        names |= set(filter(None, root.git("ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0")))
        names = sorted(n for n in names if n not in skip)
        present = [n for n in names if (Path(top) / n).is_file()]
        hashes = root.git("hash-object", "--stdin-paths", input="\n".join(present).encode(), check=False).split() if present else []
        known = dict(zip(present, hashes))
        return {n: known.get(n) for n in names}

    def identity(self, exclude: set[str] | None = None) -> tuple[str, str, str | None] | None:
        """(code id, commit, branch) for what this checkout would test now, or None outside git."""
        sha = self.resolve("HEAD")
        if not sha:
            return None
        branch = self.git("branch", "--show-current", check=False).strip() or None
        return code_identity(sha, self.dirty(exclude)), sha, branch

    def ok(self) -> bool:
        try:
            return self.git("rev-parse", "--is-inside-work-tree").strip() == "true"
        except GitError:
            return False

    def resolve(self, ref: str) -> str | None:
        out = self.git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False).strip()
        return out or None

    def branch_refs(self) -> list[tuple[str, str]]:
        """(full ref, short branch name) for local and remote-tracking branches, local first."""
        refs = []
        for line in self.git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").split():
            if line.endswith("/HEAD"):
                continue
            if line.startswith("refs/heads/"):
                refs.append((line, line[len("refs/heads/"):]))
            else:
                parts = line.split("/", 3)  # refs/remotes/<remote>/<branch>
                if len(parts) == 4:
                    refs.append((line, parts[3]))
        return refs

    def default_branch(self) -> str:
        """origin's default branch if known, else main, master or HEAD."""
        head = self.git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
        if head:
            return head
        for ref in ("refs/remotes/origin/main", "refs/heads/main", "refs/remotes/origin/master", "refs/heads/master"):
            if self.resolve(ref):
                return ref
        return "HEAD"

    def ls_dir(self, ref: str, directory: str) -> dict[str, str]:
        """{file name: blob sha} directly inside `directory` on `ref`."""
        out = {}
        for line in self.git("ls-tree", ref, "--", directory.rstrip("/") + "/", check=False).splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                out[Path(path).name] = parts[2]
        return out

    def ls_tree(self, ref: str, directory: str) -> dict[str, str]:
        """{path: blob sha} for every file under `directory` on `ref`, recursively."""
        out = {}
        for line in self.git("ls-tree", "-r", ref, "--", directory.rstrip("/") + "/", check=False).splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                out[path] = parts[2]
        return out

    def fetch(self, remote: str, branch: str) -> bool:
        """Fetch one branch into refs/remotes/<remote>/<branch>. False if it isn't there or the network is."""
        out = subprocess.run(["git", "-C", self.path, "fetch", "-q", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
                             capture_output=True)
        return out.returncode == 0

    def read_blobs(self, shas: list[str]) -> dict[str, bytes]:
        """Contents of many blobs in one `git cat-file --batch` call."""
        if not shas:
            return {}
        raw = subprocess.run(["git", "-C", self.path, "cat-file", "--batch"], input="\n".join(shas).encode() + b"\n",
                             capture_output=True, check=True).stdout
        out: dict[str, bytes] = {}
        pos = 0
        while pos < len(raw):
            nl = raw.index(b"\n", pos)
            header = raw[pos:nl].decode().split()
            pos = nl + 1
            if len(header) < 3 or header[1] == "missing":
                continue
            size = int(header[2])
            out[header[0]] = raw[pos:pos + size]
            pos += size + 1
        return out

    def show(self, ref: str, path: str) -> str | None:
        out = subprocess.run(["git", "-C", self.path, "show", f"{ref}:{path}"], capture_output=True)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None

    def commits_between(self, base: str | None, head: str, limit: int = 2000) -> list[tuple[str, str]]:
        """(sha, author date ISO) for commits reachable from head and not from base, newest first."""
        spec = [f"{base}..{head}"] if base else [head]
        out = self.git("log", f"--max-count={limit}", "--format=%H %aI", *spec, "--", check=False)
        return [tuple(line.split(" ", 1)) for line in out.splitlines() if " " in line]  # type: ignore[misc]

    def file_additions(self, ref: str, pattern: str) -> list[tuple[str, str, str]]:
        """(commit sha, commit date ISO, path) for every file matching the pathspec glob added on ref's
        first-parent history, oldest first."""
        out = self.git("log", "--first-parent", "--diff-filter=A", "--reverse", "--format=@%H %cI", "--name-only",
                       ref, "--", f":(glob){pattern}", check=False)
        rows, sha, when = [], None, None
        for line in out.splitlines():
            if line.startswith("@"):
                sha, when = line[1:].split(" ", 1)
            elif line.strip() and sha:
                rows.append((sha, when, line.strip()))
        return rows
