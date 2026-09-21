"""The running tpk version (#85). One source of truth: the release tag.

Resolution order:
  1. TPK_VERSION / TPK_COMMIT -- baked into the published images by the Docker
     workflow from the git tag (the build context has no .git, so the image
     cannot work it out for itself).
  2. `git describe` -- a dev checkout, e.g. `0.0.5-3-gbc549ef-dirty`.
  3. `dev` -- no metadata at all. Never a stale hand-edited constant: the
     `version` in pyproject.toml is a placeholder and is not reported anywhere.
"""

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def get_version() -> tuple[str, str]:
    """(version, short commit) -- either may be "" / "dev" when unknown."""
    version = os.environ.get("TPK_VERSION", "").strip()
    commit = os.environ.get("TPK_COMMIT", "").strip()
    if not version:
        version = _git("describe", "--tags", "--always", "--dirty")
        commit = commit or _git("rev-parse", "HEAD")
    return (version.removeprefix("v") or "dev", commit[:7])


def version_string() -> str:
    """`0.0.5 (bc549ef)`, or just the version when the commit is unknown."""
    version, commit = get_version()
    return f"{version} ({commit})" if commit else version
