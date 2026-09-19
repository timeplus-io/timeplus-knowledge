import asyncio
import threading

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


def test_kg_call_does_not_run_on_the_event_loop():
    """stdio: the KG call is blocking DB/file work, so it must not sit on the
    event-loop thread (which would stall the whole server for its duration)."""
    class ThreadRecordingKG(FakeKG):
        worker_thread = None

        def search_entities(self, query, kinds=None, repos=None, limit=20):
            ThreadRecordingKG.worker_thread = threading.get_ident()
            return []

    server = build_server(ThreadRecordingKG())

    async def call():
        await server.call_tool("search_entities", {"query": "x"})
        return threading.get_ident()

    loop_thread = asyncio.run(call())
    assert ThreadRecordingKG.worker_thread not in (None, loop_thread)
