from pathlib import Path

from tpk.agent import RECURSION_LIMIT, build_agent, system_prompt
from tpk.config import AgentConfig, RepoConfig

REPOS = {
    "proton": RepoConfig(
        name="proton", path=Path("/r/proton"), visibility="internal",
        description="Core streaming SQL engine",
    ),
    "docs": RepoConfig(
        name="docs", path=Path("/r/docs"), visibility="public",
        extraction="semantic", description="Public product documentation",
    ),
}


class FakeKG:
    def search_entities(self, query, kinds=None, repos=None, limit=20):
        return []

    def get_entity(self, entity_id):
        return None

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        return {"nodes": [], "edges": [], "depth_used": depth}

    def path_between(self, id_a, id_b, max_depth=4):
        return None

    def list_communities(self, repo=None):
        return []

    def read_source(self, repo, file_path, line_start, line_end):
        return ""


def test_system_prompt_contains_corpus_and_citation_rules():
    p = system_prompt(REPOS)
    assert "proton" in p and "Core streaming SQL engine" in p
    assert "docs" in p and "Public product documentation" in p
    assert "repo/file_path:line" in p
    assert "could not find this in the knowledge graph" in p
    # Graph traversal is encouraged for exploration generally, not gated to
    # explicit "what calls X" questions.
    assert "LEAN ON THE GRAPH" in p
    assert "neighbors" in p and "path_between" in p


def test_recursion_limit_constant():
    assert RECURSION_LIMIT == 40


def test_build_agent_answers_via_fake_model(monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    fake = GenericFakeChatModel(messages=iter([AIMessage(content="grounded answer")]))
    # GenericFakeChatModel.bind_tools raises NotImplementedError on the
    # installed langchain-core (and it's a pydantic model, so instance
    # attributes can't be set directly); the test verifies wiring, not
    # tool-calling, so patch the class method to return the model as-is.
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **k: self)
    agent = build_agent(
        FakeKG(), AgentConfig(provider="openai", model="x"), REPOS, model=fake
    )
    result = agent.invoke({"messages": [("user", "what is proton?")]})
    assert result["messages"][-1].content == "grounded answer"


def test_system_prompt_labels_versioned_entries():
    from tpk.config import RepoConfig

    entries = [RepoConfig(name="proton-enterprise", github="o/pe", ref="v3.3.1",
                          visibility="internal", description="Enterprise engine")]
    p = system_prompt(entries)
    assert "proton-enterprise@v3.3.1" in p
    assert "Enterprise engine" in p


def test_build_agent_with_corpus_provider_renders_live_prompt(monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from tpk.config import RepoConfig

    fake = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **k: self)
    seen = []

    def provider():
        seen.append(1)
        return [RepoConfig(name="live", github="o/l", ref="v9", visibility="public",
                           description="live entry")]

    agent = build_agent(
        FakeKG(), AgentConfig(provider="openai", model="x"), {},
        model=fake, corpus_provider=provider,
    )
    result = agent.invoke({"messages": [("user", "hi")]})
    assert result["messages"][-1].content == "ok"
    assert seen  # provider consulted during invocation


def test_build_agent_falls_back_to_seed_corpus_when_provider_raises(monkeypatch):
    """A transient corpus_provider failure (e.g. DB hiccup) must not kill the
    chat turn -- the agent should still answer, using the static `repos`
    passed to build_agent as the last-known-good corpus."""
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    seen_messages = []

    class RecordingFakeChatModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen_messages.append(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    fake = RecordingFakeChatModel(messages=iter([AIMessage(content="fallback ok")]))
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **k: self)

    def broken_provider():
        raise RuntimeError("db unavailable")

    agent = build_agent(
        FakeKG(), AgentConfig(provider="openai", model="x"), REPOS,
        model=fake, corpus_provider=broken_provider,
    )
    result = agent.invoke({"messages": [("user", "hi")]})

    assert result["messages"][-1].content == "fallback ok"
    system_text = seen_messages[0][0].content
    assert "proton" in system_text and "Core streaming SQL engine" in system_text
