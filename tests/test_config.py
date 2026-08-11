from pathlib import Path

import pytest

from tpk.config import Settings, load_llm, load_repos


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


def test_load_repos_extraction_and_description(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\npath = "/x/docs"\nvisibility = "public"\n'
        'extraction = "semantic"\ndescription = "Public docs"\n\n'
        '[repos.proton]\npath = "/x/proton"\nvisibility = "internal"\n'
    )
    repos = load_repos(toml_path)
    assert repos["docs"].extraction == "semantic"
    assert repos["docs"].description == "Public docs"
    assert repos["proton"].extraction == "code-only"
    assert repos["proton"].description == ""


def test_load_repos_rejects_bad_extraction(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.x]\npath = "/x"\nvisibility = "internal"\nextraction = "magic"\n'
    )
    with pytest.raises(ValueError):
        load_repos(toml_path)


def test_load_llm_default_and_explicit(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos.x]\npath = "/x"\nvisibility = "internal"\n')
    assert load_llm(toml_path).backend == "auto"
    toml_path.write_text(
        '[llm]\nbackend = "claude"\n\n[repos.x]\npath = "/x"\nvisibility = "internal"\n'
    )
    assert load_llm(toml_path).backend == "claude"


def test_load_llm_rejects_bad_backend(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[llm]\nbackend = "grok"\n')
    with pytest.raises(ValueError):
        load_llm(toml_path)


def test_load_llm_model(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[llm]\nbackend = "openai"\nmodel = "gpt-5.2"\n')
    llm = load_llm(toml_path)
    assert llm.model == "gpt-5.2"
    toml_path.write_text('[repos.x]\npath = "/x"\nvisibility = "internal"\n')
    assert load_llm(toml_path).model == ""


def test_load_llm_token_budget(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[llm]\nbackend = "openai"\ntoken_budget = 16000\n')
    assert load_llm(toml_path).token_budget == 16000
    toml_path.write_text('[repos.x]\npath = "/x"\nvisibility = "internal"\n')
    assert load_llm(toml_path).token_budget == 0


def test_load_repos_github_source(tmp_path: Path, monkeypatch):
    from tpk.config import repo_paths

    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\ngithub = "timeplus-io/docs"\nref = "main"\nvisibility = "public"\n'
    )
    repos = load_repos(toml_path)
    assert repos["docs"].github == "timeplus-io/docs"
    assert repos["docs"].ref == "main"
    assert repos["docs"].path is None
    monkeypatch.setenv("TPK_CHECKOUT_DIR", str(tmp_path / "cache"))
    assert repo_paths(repos)["docs"] == tmp_path / "cache" / "docs" / "main"


def test_load_repos_rejects_both_or_neither_source(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.x]\npath = "/x"\ngithub = "o/r"\nref = "v1"\nvisibility = "internal"\n'
    )
    with pytest.raises(ValueError, match="exactly one"):
        load_repos(toml_path)
    toml_path.write_text('[repos.x]\nvisibility = "internal"\n')
    with pytest.raises(ValueError, match="exactly one"):
        load_repos(toml_path)


def test_load_repos_github_requires_ref(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos.x]\ngithub = "o/r"\nvisibility = "internal"\n')
    with pytest.raises(ValueError, match="ref"):
        load_repos(toml_path)


def test_entry_key_and_enabled_default(tmp_path: Path):
    from tpk.config import entry_key

    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\ngithub = "timeplus-io/docs"\nref = "main"\nvisibility = "public"\n\n'
        '[repos.local]\npath = "/x"\nvisibility = "internal"\n'
    )
    repos = load_repos(toml_path)
    assert repos["docs"].enabled is True
    assert entry_key(repos["docs"]) == "docs@main"
    assert entry_key(repos["local"]) == "local"


def test_load_repos_rejects_at_sign_in_name(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos."bad@name"]\npath = "/x"\nvisibility = "internal"\n')
    with pytest.raises(ValueError, match="@"):
        load_repos(toml_path)
