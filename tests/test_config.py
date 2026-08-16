from pathlib import Path

import pytest

from tpk.config import (
    AgentConfig,
    Settings,
    as_bool,
    checkout_root,
    db_backend,
    load_llm,
    load_repos,
    setting,
)


# -- env > file > default precedence (issue #54) -----------------------------


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Point config_path()/setting() at a writable temp TOML via TPK_CONFIG."""
    path = tmp_path / "config.toml"
    path.write_text("")
    monkeypatch.setenv("TPK_CONFIG", str(path))
    return path


def test_config_path_honours_tpk_config(tmp_path, monkeypatch):
    from tpk.config import config_path

    monkeypatch.delenv("TPK_CONFIG", raising=False)
    assert config_path().name == "repos.toml"
    custom = tmp_path / "custom.toml"
    monkeypatch.setenv("TPK_CONFIG", str(custom))
    assert config_path() == custom


def test_modules_resolve_repos_toml_via_config_path(tmp_path, monkeypatch):
    """server/api/mcp_server must honour TPK_CONFIG like cli does, so the whole
    app reads one config file (regression: they hardcoded repos.toml)."""
    import importlib

    mod_names = ("tpk.cli", "tpk.server", "tpk.api", "tpk.mcp_server")
    custom = tmp_path / "custom.toml"
    custom.write_text("")
    monkeypatch.setenv("TPK_CONFIG", str(custom))
    try:
        for mod_name in mod_names:
            mod = importlib.reload(importlib.import_module(mod_name))
            assert mod.REPOS_TOML == custom, f"{mod_name}.REPOS_TOML ignored TPK_CONFIG"
    finally:
        # Reload without TPK_CONFIG so the module-level REPOS_TOML constants
        # don't leak the temp path into other tests.
        monkeypatch.delenv("TPK_CONFIG", raising=False)
        for mod_name in mod_names:
            importlib.reload(importlib.import_module(mod_name))


def test_setting_prefers_default_when_unset(config_file, monkeypatch):
    monkeypatch.delenv("TPK_X", raising=False)
    assert setting("TPK_X", "db", "x", "fallback") == "fallback"


def test_setting_reads_file_over_default(config_file, monkeypatch):
    monkeypatch.delenv("TPK_X", raising=False)
    config_file.write_text('[db]\nx = "from_file"\n')
    assert setting("TPK_X", "db", "x", "fallback") == "from_file"


def test_setting_env_wins_over_file(config_file, monkeypatch):
    config_file.write_text('[db]\nx = "from_file"\n')
    monkeypatch.setenv("TPK_X", "from_env")
    assert setting("TPK_X", "db", "x", "fallback") == "from_env"


def test_setting_empty_env_counts_as_unset(config_file, monkeypatch):
    config_file.write_text('[db]\nx = "from_file"\n')
    monkeypatch.setenv("TPK_X", "")
    assert setting("TPK_X", "db", "x", "fallback") == "from_file"


def test_setting_casts_file_and_env(config_file, monkeypatch):
    config_file.write_text("[db]\nn = 7\n")
    monkeypatch.delenv("TPK_N", raising=False)
    assert setting("TPK_N", "db", "n", 0, cast=int) == 7
    monkeypatch.setenv("TPK_N", "9")
    assert setting("TPK_N", "db", "n", 0, cast=int) == 9


def test_setting_tolerates_missing_or_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TPK_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.delenv("TPK_X", raising=False)
    assert setting("TPK_X", "db", "x", "fallback") == "fallback"
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not [ valid toml")
    monkeypatch.setenv("TPK_CONFIG", str(bad))
    assert setting("TPK_X", "db", "x", "fallback") == "fallback"


def test_as_bool():
    assert as_bool(True) is True
    assert as_bool("1") is True and as_bool("true") is True and as_bool("yes") is True
    assert as_bool(False) is False
    for falsy in ("0", "false", "False", "no", "off", ""):
        assert as_bool(falsy) is False


def test_db_backend_precedence(config_file, monkeypatch):
    monkeypatch.delenv("TPK_DB_BACKEND", raising=False)
    assert db_backend() == "timeplusd"
    config_file.write_text('[db]\nbackend = "proton"\n')
    assert db_backend() == "proton"
    monkeypatch.setenv("TPK_DB_BACKEND", "timeplusd")
    assert db_backend() == "timeplusd"


def test_db_backend_rejects_bad_value(config_file, monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "sqlite")
    with pytest.raises(ValueError):
        db_backend()


def test_settings_reads_db_section_from_file(config_file, monkeypatch):
    for var in ("TIMEPLUS_HOST", "TIMEPLUS_USER", "TPK_STREAM_PREFIX", "TPK_DB_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TIMEPLUS_PASSWORD", raising=False)
    config_file.write_text(
        '[db]\nhost = "db.internal"\nuser = "tpk"\nstream_prefix = "p_"\nbackend = "proton"\n'
    )
    s = Settings.from_env()
    assert (s.host, s.user, s.stream_prefix, s.backend) == ("db.internal", "tpk", "p_", "proton")
    # env still wins over the file
    monkeypatch.setenv("TIMEPLUS_HOST", "override")
    assert Settings.from_env().host == "override"


def test_checkout_root_precedence(config_file, monkeypatch):
    monkeypatch.delenv("TPK_CHECKOUT_DIR", raising=False)
    config_file.write_text('[server]\ncheckout_dir = "/data/checkouts"\n')
    assert checkout_root() == Path("/data/checkouts")
    monkeypatch.setenv("TPK_CHECKOUT_DIR", "/env/checkouts")
    assert checkout_root() == Path("/env/checkouts")


def test_agent_config_reads_file(config_file, monkeypatch):
    for var in ("TPK_AGENT_PROVIDER", "TPK_AGENT_MODEL", "TPK_AGENT_REASONING_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    config_file.write_text(
        '[agent]\nprovider = "openai"\nmodel = "gpt-5.2"\nreasoning_effort = "low"\n'
    )
    cfg = AgentConfig.from_env()
    assert (cfg.provider, cfg.model) == ("openai", "gpt-5.2")
    assert AgentConfig.reasoning_effort() == "low"
    # env wins
    monkeypatch.setenv("TPK_AGENT_MODEL", "gpt-override")
    assert AgentConfig.from_env().model == "gpt-override"


def test_settings_from_env_defaults(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_HOST", raising=False)
    monkeypatch.delenv("TIMEPLUS_USER", raising=False)
    monkeypatch.delenv("TIMEPLUS_PASSWORD", raising=False)
    monkeypatch.delenv("TPK_STREAM_PREFIX", raising=False)
    monkeypatch.delenv("TPK_CONFIG", raising=False)
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
