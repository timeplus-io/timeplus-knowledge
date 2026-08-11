import pytest

from tpk.config import AgentConfig

ENV_VARS = (
    "TPK_AGENT_PROVIDER", "TPK_AGENT_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def test_explicit_provider_and_model(monkeypatch):
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("TPK_AGENT_MODEL", "openai.gpt-oss-120b")
    cfg = AgentConfig.from_env()
    assert (cfg.provider, cfg.model) == ("openai", "openai.gpt-oss-120b")


def test_provider_inferred_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    assert AgentConfig.from_env().provider == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # anthropic preferred when both
    assert AgentConfig.from_env().provider == "anthropic"


def test_default_models(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert AgentConfig.from_env().model == "claude-sonnet-5"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert AgentConfig.from_env().model == "gpt-5.2"


def test_no_provider_signal_raises():
    with pytest.raises(ValueError, match="TPK_AGENT_PROVIDER"):
        AgentConfig.from_env()


def test_bad_provider_raises(monkeypatch):
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "grok")
    with pytest.raises(ValueError):
        AgentConfig.from_env()


def test_build_chat_model_openai_gateway(monkeypatch):
    from tpk.agent import build_chat_model
    from tpk.config import AgentConfig

    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    model = build_chat_model(AgentConfig(provider="openai", model="openai.gpt-oss-120b"))
    assert model.model_name == "openai.gpt-oss-120b"
    assert "gw" in str(model.openai_api_base)


def test_build_chat_model_anthropic(monkeypatch):
    from tpk.agent import build_chat_model
    from tpk.config import AgentConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    model = build_chat_model(AgentConfig(provider="anthropic", model="claude-sonnet-5"))
    assert model.model == "claude-sonnet-5"
