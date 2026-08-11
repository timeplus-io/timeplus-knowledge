"""Fetch GitHub-sourced repos at a pinned ref into the checkout cache.

Auth: if GITHUB_TOKEN is set, git receives it through an in-process
credential helper (the token never appears in argv or error output);
otherwise plain https is used (public repos, or a host git credential
helper such as gh/osxkeychain).
"""

import os
import subprocess
from pathlib import Path

from tpk.config import RepoConfig, resolved_repo_path


class FetchError(RuntimeError):
    pass


_HELPER = '!f() { echo "username=x-access-token"; echo "password=$GITHUB_TOKEN"; }; f'


def _git(args: list[str], cwd: Path | None = None) -> str:
    cmd = ["git"]
    if os.environ.get("GITHUB_TOKEN"):
        cmd += ["-c", f"credential.helper={_HELPER}"]
    cmd += args
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(f"git {args[0]} failed: {proc.stderr[-500:]}")
    return proc.stdout


def clone_url(github: str) -> str:
    return f"https://github.com/{github}.git"


def fetch_github_repo(cfg: RepoConfig) -> Path:
    """Clone (or update) cfg.github at cfg.ref; returns the checkout path.

    Tag refs are effectively immutable: an existing checkout is refreshed
    with a cheap shallow fetch + reset, which is a no-op for unchanged tags
    and picks up new commits for branch refs.
    """
    dest = resolved_repo_path(cfg)
    if (dest / ".git").is_dir():
        _git(["fetch", "--depth", "1", "origin", cfg.ref], cwd=dest)
        _git(["reset", "--hard", "FETCH_HEAD"], cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "1", "--branch", cfg.ref, clone_url(cfg.github), str(dest)])
    return dest
