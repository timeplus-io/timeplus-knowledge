from pathlib import Path

from tpk.config import Settings, load_repos


def test_settings_from_env_defaults(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_HOST", raising=False)
    monkeypatch.delenv("TIMEPLUS_USER", raising=False)
    monkeypatch.delenv("TIMEPLUS_PASSWORD", raising=False)
    s = Settings.from_env()
    assert s.host == "localhost"
    assert s.user == "default"
    assert s.password == ""
    assert s.port == 8123
    assert s.stream_prefix == ""


def test_settings_from_env_reads_vars(monkeypatch):
    monkeypatch.setenv("TIMEPLUS_HOST", "tp.example.com")
    monkeypatch.setenv("TIMEPLUS_USER", "eng")
    monkeypatch.setenv("TIMEPLUS_PASSWORD", "secret")
    s = Settings.from_env()
    assert (s.host, s.user, s.password) == ("tp.example.com", "eng", "secret")


def test_load_repos(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\npath = "/x/docs"\nvisibility = "public"\n\n'
        '[repos.proton]\npath = "/x/proton"\nvisibility = "internal"\n'
    )
    repos = load_repos(toml_path)
    assert set(repos) == {"docs", "proton"}
    assert repos["docs"].visibility == "public"
    assert repos["proton"].path == Path("/x/proton")
