import subprocess
from pathlib import Path

import pytest

import tpk.fetch as fetch_mod
from tpk.config import RepoConfig
from tpk.fetch import FetchError, fetch_github_repo


def _make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    def g(*args):
        subprocess.run(["git", "-C", str(origin), *args], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(origin)], check=True, capture_output=True)
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (origin / "a.py").write_text("def one():\n    return 1\n")
    g("add", ".")
    g("commit", "-q", "-m", "v1")
    g("tag", "v1.0.0")
    (origin / "a.py").write_text("def two():\n    return 2\n")
    g("commit", "-aq", "-m", "v2")
    return origin


@pytest.fixture()
def origin(tmp_path, monkeypatch):
    o = _make_origin(tmp_path)
    monkeypatch.setenv("TPK_CHECKOUT_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(fetch_mod, "clone_url", lambda github: str(o))
    return o


def test_fetch_clones_pinned_tag(origin, tmp_path):
    cfg = RepoConfig(name="r", github="fake/repo", ref="v1.0.0")
    dest = fetch_github_repo(cfg)
    assert dest == tmp_path / "cache" / "r" / "v1.0.0"
    assert "def one" in (dest / "a.py").read_text()  # tag content, not tip


def test_fetch_reuses_and_updates_branch_ref(origin, tmp_path):
    branch = subprocess.run(
        ["git", "-C", str(origin), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    cfg = RepoConfig(name="r", github="fake/repo", ref=branch)
    dest = fetch_github_repo(cfg)
    assert "def two" in (dest / "a.py").read_text()
    # advance origin; re-fetch must pick it up in the SAME cache dir
    (origin / "a.py").write_text("def three():\n    return 3\n")
    subprocess.run(["git", "-C", str(origin), "commit", "-aq", "-m", "v3"], check=True)
    assert fetch_github_repo(cfg) == dest
    assert "def three" in (dest / "a.py").read_text()


def test_fetch_bad_ref_raises(origin):
    with pytest.raises(FetchError):
        fetch_github_repo(RepoConfig(name="r", github="fake/repo", ref="no-such-tag"))


def test_fetch_rejects_ref_starting_with_dash(origin):
    # Defense in depth against git-option injection via `ref` (the API
    # route validates this too, but the fetch call site must not trust it
    # blindly): a leading '-' is rejected before git ever sees it, on both
    # the fresh-clone and existing-checkout-update paths (the guard runs
    # before either branch).
    with pytest.raises(ValueError):
        fetch_github_repo(RepoConfig(name="r", github="fake/repo", ref="--upload-pack=x"))
