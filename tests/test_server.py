import json

from fastapi.testclient import TestClient

from tpk.server import create_app


class FakeAgent:
    """Yields the astream_events shapes the server consumes."""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, _input, version="v2", config=None):
        for e in self._events:
            yield e


def _tok(text):
    class Chunk:
        content = text

    return {"event": "on_chat_model_stream", "data": {"chunk": Chunk()}}


def _tool(name, inp):
    return {"event": "on_tool_start", "name": name, "data": {"input": inp}}


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def test_healthz():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_streams_tokens_tools_and_done():
    agent = FakeAgent([_tool("search_entities", {"query": "q"}), _tok("Hello "), _tok("world")])
    client = TestClient(create_app(agent=agent))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert {"type": "tool", "name": "search_entities", "input": {"query": "q"}} in events
    assert {"type": "token", "text": "Hello "} in events
    assert events[-1] == {"type": "done", "text": "Hello world"}


def test_chat_streams_error_event():
    class BoomAgent:
        async def astream_events(self, _input, version="v2", config=None):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    client = TestClient(create_app(agent=BoomAgent()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert events[-1]["type"] == "error"
    assert "RuntimeError" in events[-1]["message"]
    assert "model exploded" not in events[-1]["message"]


def test_chat_rejects_empty_message():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_rejects_invalid_history_role():
    client = TestClient(create_app(agent=FakeAgent([])))
    resp = client.post(
        "/chat",
        json={"message": "hi", "history": [{"role": "system", "content": "x"}]},
    )
    assert resp.status_code == 422
