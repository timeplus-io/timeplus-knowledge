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
