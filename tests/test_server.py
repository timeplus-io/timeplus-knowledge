import json

from fastapi.testclient import TestClient

from tpk.auth import AuthLayer, User
from tpk.server import create_app


class _StubAuth(AuthLayer):
    """Grants a fixed user without touching any store."""

    def __init__(self, user: User | None = None):
        super().__init__()
        self.user = user or User("tester", "", "admin")

    def _resolve(self, authorization):
        return self.user

    def _client(self):
        # Every other test in this file is deliberately infra-free
        # (_resolve never touches a store); the /chat handler's own
        # role-scope lookup calls `auth._client()` directly for non-admin
        # users, so this must fail immediately too -- a real
        # `db.get_client()` call here would reach out over the network
        # (and, against an unreachable-but-not-refusing host, block for a
        # full TCP connect timeout) on every non-admin /chat request in
        # this file's tests.
        raise RuntimeError("no store in unit tests")


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
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_streams_tokens_tools_and_done():
    agent = FakeAgent([_tool("search_entities", {"query": "q"}), _tok("Hello "), _tok("world")])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
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

    client = TestClient(create_app(agent=BoomAgent(), auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert events[-1]["type"] == "error"
    assert "RuntimeError" in events[-1]["message"]
    assert "model exploded" not in events[-1]["message"]


def test_chat_rejects_empty_message():
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_rejects_invalid_history_role():
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    resp = client.post(
        "/chat",
        json={"message": "hi", "history": [{"role": "system", "content": "x"}]},
    )
    assert resp.status_code == 422


def _end(text):
    class Msg:
        content = text

    return {"event": "on_chat_model_end", "data": {"output": Msg()}}


def test_done_prefers_final_model_message():
    agent = FakeAgent([_tok("Let me search... "), _end("The grounded final answer.")])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert events[-1] == {"type": "done", "text": "The grounded final answer."}


def test_done_fallback_when_model_emits_no_text():
    agent = FakeAgent([_tool("search_entities", {"query": "q"})])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert events[-1]["type"] == "done"
    assert "retry" in events[-1]["text"]


def test_chat_401_without_auth():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_sets_and_resets_role_scope():
    """Non-admin user, role "viewer" -- _StubAuth._client() raises
    immediately (no real store reachable), so the scope must fail closed to
    frozenset() rather than falling through to unrestricted (None) access,
    with zero network I/O. FakeAgent doesn't call any tools, so this only
    exercises the set/reset bracket around stream(): after the response
    completes, ROLE_SCOPE must be back to its default (None) in this
    thread."""
    from tpk.tools import ROLE_SCOPE

    stub = _StubAuth(user=User("viewer1", "", "viewer"))
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]), auth=stub))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert ROLE_SCOPE.get() is None
