from tpk.agent_tools import build_agent_tools

EXPECTED = {
    "search_entities", "get_entity", "neighbors",
    "path_between", "list_communities", "read_source",
}


class FakeKG:
    def __init__(self):
        self.calls = []

    def search_entities(self, query, kinds=None, repos=None, limit=20):
        self.calls.append(("search_entities", query))
        return [{"id": "x", "name": query}]

    def get_entity(self, entity_id):
        return {"id": entity_id}

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        return {"nodes": [], "edges": [], "depth_used": depth}

    def path_between(self, id_a, id_b, max_depth=8, mode="auto"):
        return {"found": False, "mode": None, "direction": None, "path": [], "note": ""}

    def list_communities(self, repo=None, limit=None, min_nodes=1):
        self.communities_call = {"repo": repo, "limit": limit, "min_nodes": min_nodes}
        return {"communities": [], "total": 0, "returned": 0, "truncated": False}

    def read_source(self, repo, file_path, line_start, line_end):
        if file_path == "boom.py":
            raise RuntimeError("boom")
        return "line"


def test_six_tools_with_exact_names():
    tools = build_agent_tools(FakeKG())
    assert {t.name for t in tools} == EXPECTED
    assert all(t.description for t in tools)  # every tool documents itself


def test_tools_delegate_to_kg():
    kg = FakeKG()
    tools = {t.name: t for t in build_agent_tools(kg)}
    out = tools["search_entities"].invoke({"query": "checkpoint"})
    assert out == [{"id": "x", "name": "checkpoint"}]
    assert kg.calls == [("search_entities", "checkpoint")]
    assert tools["get_entity"].invoke({"entity_id": "abc"}) == {"id": "abc"}
    assert tools["read_source"].invoke(
        {"repo": "r", "file_path": "f.py", "line_start": 1, "line_end": 2}
    ) == "line"


def test_tool_exception_becomes_structured_error_string():
    """A KG method raising must not propagate out of the tool -- it must come
    back as a string the model can read and react to (spec: "tool errors
    return structured error strings to the LLM so it can retry or degrade")."""
    kg = FakeKG()
    tools = {t.name: t for t in build_agent_tools(kg)}
    out = tools["read_source"].invoke(
        {"repo": "r", "file_path": "boom.py", "line_start": 1, "line_end": 2}
    )
    assert isinstance(out, str)
    assert "TOOL_ERROR" in out
    assert "boom" in out


def test_search_empty_returns_guidance_string():
    class EmptyKG(FakeKG):
        def search_entities(self, query, kinds=None, repos=None, limit=20):
            return []

    tools = {t.name: t for t in build_agent_tools(EmptyKG())}
    out = tools["search_entities"].invoke({"query": "no such thing"})
    assert isinstance(out, str) and out.startswith("NO_RESULTS")
    assert "could not find this in the knowledge graph" in out


def test_list_communities_passes_the_output_bounds_through():
    """#76: the agent can narrow/widen the overview; the KG enforces the cap."""
    kg = FakeKG()
    tools = {t.name: t for t in build_agent_tools(kg)}
    tools["list_communities"].invoke({"repo": "r1", "limit": 5, "min_nodes": 10})
    assert kg.communities_call == {"repo": "r1", "limit": 5, "min_nodes": 10}
    tools["list_communities"].invoke({})
    assert kg.communities_call == {"repo": None, "limit": None, "min_nodes": 1}
