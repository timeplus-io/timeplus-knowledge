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

    def path_between(self, id_a, id_b, max_depth=4):
        return None

    def list_communities(self, repo=None):
        return []

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
