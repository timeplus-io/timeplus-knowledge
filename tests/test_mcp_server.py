import asyncio

from tpk.mcp_server import build_server

EXPECTED_TOOLS = {
    "search_entities", "get_entity", "neighbors",
    "path_between", "list_communities", "read_source",
}


class FakeKG:
    def search_entities(self, query, kinds=None, repos=None, limit=20):
        return [{"id": "x", "name": query}]


def test_all_six_tools_registered():
    server = build_server(FakeKG())
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_tool_calls_delegate_to_kg():
    server = build_server(FakeKG())
    result = asyncio.run(server.call_tool("search_entities", {"query": "checkpoint"}))
    assert "checkpoint" in str(result)
