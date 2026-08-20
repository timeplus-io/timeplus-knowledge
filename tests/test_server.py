import json

from fastapi.testclient import TestClient

from tpk.auth import AuthLayer, User
from tpk.server import create_app


class _StubAuth(AuthLayer):
    """Grants a fixed user without touching any store."""

    def __init__(self, user: User | None = None, caps=None):
        super().__init__()
        self.user = user or User("tester", "", "admin")
        self._caps = caps

    def _resolve(self, authorization):
        return self.user

    def effective_caps(self, user):
        # Capabilities without a store: default admin -> all; a non-admin
        # stub declares its caps explicitly so require_cap gates resolve
        # without the (deliberately raising) _client().
        if self._caps is not None:
            return set(self._caps)
        from tpk.auth import ALL_CAPABILITIES
        return set(ALL_CAPABILITIES)

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


def _think(text):
    """A chunk whose content is an Anthropic-style thinking block."""
    class Chunk:
        content = [{"type": "thinking", "thinking": text}]

    return {"event": "on_chat_model_stream", "data": {"chunk": Chunk()}}


def _reason(text):
    """A chunk carrying OpenAI-compatible reasoning (gpt-oss/DeepSeek/Qwen):
    reasoning on additional_kwargs, empty content."""
    class Chunk:
        content = ""
        additional_kwargs = {"reasoning": text}

    return {"event": "on_chat_model_stream", "data": {"chunk": Chunk()}}


def _tool(name, inp):
    return {"event": "on_tool_start", "name": name, "data": {"input": inp}}


def _tool_end(name, inp, output=""):
    return {"event": "on_tool_end", "name": name, "data": {"input": inp, "output": output}}


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _end_usage(total_tokens=0, text=""):
    """An on_chat_model_end event whose output carries usage_metadata (#62)."""
    class Msg:
        content = text
        usage_metadata = {"total_tokens": total_tokens}
    return {"event": "on_chat_model_end", "data": {"output": Msg()}}


class _Usage:
    """In-memory usage store stub for enforcement/metering tests."""

    def __init__(self, used=0):
        self._used = used
        self.recorded = []

    def used_today(self, username, now=None):
        return self._used

    def record(self, username, tokens):
        self.recorded.append((username, tokens))


def test_healthz():
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_over_daily_budget_returns_429(monkeypatch):
    # No per-role limit reachable in unit tests (stub._client raises), so the
    # global fallback applies; used == limit -> blocked.
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "1000")
    monkeypatch.delenv("TPK_CONFIG", raising=False)
    usage = _Usage(used=1000)
    auth = _StubAuth(User("bob", "", "member"), caps=["chat"])
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]), auth=auth, usage=usage))
    resp = client.post("/chat", json={"message": "q"})
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["limit"] == 1000 and detail["used"] == 1000
    assert "reset" in detail and "budget" in detail["message"].lower()


def test_chat_under_budget_proceeds_and_records_usage(monkeypatch):
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "10000")
    monkeypatch.delenv("TPK_CONFIG", raising=False)
    usage = _Usage(used=500)
    auth = _StubAuth(User("bob", "", "member"), caps=["chat"])
    agent = FakeAgent([_tok("hi "), _end_usage(1234, "hi there")])
    client = TestClient(create_app(agent=agent, auth=auth, usage=usage))
    resp = client.post("/chat", json={"message": "q"})
    assert resp.status_code == 200
    # the turn's token cost is metered to the store
    assert usage.recorded == [("bob", 1234)]


def test_chat_admin_never_limited(monkeypatch):
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "10")
    usage = _Usage(used=10_000_000)  # way over, but admin is exempt
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]),
                                   auth=_StubAuth(User("root", "", "admin")), usage=usage))
    resp = client.post("/chat", json={"message": "q"})
    assert resp.status_code == 200


def test_chat_no_usage_store_skips_enforcement(monkeypatch):
    # Unit tests that don't inject a usage store must not enforce (or touch a DB).
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "1")
    auth = _StubAuth(User("bob", "", "member"), caps=["chat"])
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]), auth=auth))
    assert client.post("/chat", json={"message": "q"}).status_code == 200


def test_chat_user_override_wins_over_global(monkeypatch):
    # A tight per-user override beats a generous global default.
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "1000000")
    monkeypatch.delenv("TPK_CONFIG", raising=False)
    usage = _Usage(used=100)
    user = User("bob", "", "member", daily_token_limit=100)
    auth = _StubAuth(user, caps=["chat"])
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]), auth=auth, usage=usage))
    resp = client.post("/chat", json={"message": "q"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["limit"] == 100


def test_chat_usage_status_reflects_user_override(monkeypatch):
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "1000000")
    monkeypatch.delenv("TPK_CONFIG", raising=False)
    usage = _Usage(used=40)
    user = User("bob", "", "member", daily_token_limit=100)
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth(user, caps=["chat"]), usage=usage))
    body = client.get("/chat/usage").json()
    assert body["limit"] == 100 and body["remaining"] == 60


def test_chat_usage_status_for_limited_user(monkeypatch):
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "50000")
    monkeypatch.delenv("TPK_CONFIG", raising=False)
    usage = _Usage(used=1234)
    auth = _StubAuth(User("bob", "", "member"), caps=["chat"])
    client = TestClient(create_app(agent=FakeAgent([]), auth=auth, usage=usage))
    body = client.get("/chat/usage").json()
    assert body["limited"] is True
    assert body["used"] == 1234 and body["limit"] == 50000
    assert body["remaining"] == 48766 and "reset" in body


def test_chat_usage_status_unlimited_for_admin_and_no_store(monkeypatch):
    monkeypatch.setenv("TPK_DAILY_TOKEN_LIMIT", "50000")
    # admin -> unlimited even with a store
    admin = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth(User("root", "", "admin")),
                                  usage=_Usage(used=9)))
    assert admin.get("/chat/usage").json() == {"limited": False}
    # non-admin but no usage store -> unlimited (unit path)
    auth = _StubAuth(User("bob", "", "member"), caps=["chat"])
    nostore = TestClient(create_app(agent=FakeAgent([]), auth=auth))
    assert nostore.get("/chat/usage").json() == {"limited": False}


def test_chat_model_endpoint(monkeypatch):
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("TPK_AGENT_MODEL", "anthropic.claude-opus-4-8")
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    body = client.get("/chat/model").json()
    assert body == {"provider": "anthropic", "model": "anthropic.claude-opus-4-8"}


def test_chat_model_endpoint_unconfigured(monkeypatch):
    monkeypatch.delenv("TPK_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("TPK_AGENT_MODEL", raising=False)
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    client = TestClient(create_app(agent=FakeAgent([]), auth=_StubAuth()))
    assert client.get("/chat/model").json() == {"provider": None, "model": None}


def test_chat_streams_tokens_tools_and_done():
    agent = FakeAgent([_tool("search_entities", {"query": "q"}), _tok("Hello "), _tok("world")])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert {"type": "tool", "name": "search_entities", "input": {"query": "q"}} in events
    assert {"type": "token", "text": "Hello "} in events
    assert events[-1] == {"type": "done", "text": "Hello world", "sources": []}


def test_chat_streams_thinking_interleaved_with_tools():
    """Thinking deltas are surfaced as `thinking` events, interleaved with
    tool calls in stream order; the answer text is unaffected."""
    agent = FakeAgent([
        _think("Two candidate causes. "), _think("Checking the coordinator. "),
        _tool("search_entities", {"query": "checkpoint"}),
        _think("Confirmed the resume point. "),
        _tok("The view "), _tok("replays."),
    ])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    resp = client.post("/chat", json={"message": "hi"})
    events = _parse_sse(resp.text)
    thinking = [e for e in events if e["type"] == "thinking"]
    assert [e["text"] for e in thinking] == [
        "Two candidate causes. ", "Checking the coordinator. ", "Confirmed the resume point. "]
    # order preserved: first two thinking events precede the tool event
    tool_idx = next(i for i, e in enumerate(events) if e["type"] == "tool")
    think_before = [i for i, e in enumerate(events) if e["type"] == "thinking" and i < tool_idx]
    assert len(think_before) == 2
    assert events[-1] == {"type": "done", "text": "The view replays.", "sources": []}


def test_chat_streams_openai_reasoning_as_thinking():
    """OpenAI-compatible reasoning (additional_kwargs.reasoning) is surfaced as
    thinking events, same as Anthropic thinking blocks."""
    agent = FakeAgent([
        _reason("91 = 7*13, so "), _reason("it's composite."),
        _tok("91 is not prime."),
    ])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert [e["text"] for e in events if e["type"] == "thinking"] == [
        "91 = 7*13, so ", "it's composite."]
    assert events[-1] == {"type": "done", "text": "91 is not prime.", "sources": []}


def test_chat_emits_search_entities_match_count():
    """search_entities' on_tool_end emits a tool_result with the match count.

    In a real LangGraph run the tool's list return is wrapped by ToolNode in a
    ToolMessage whose `.content` is a JSON *string* (json.dumps), not a list —
    so the count must be decoded from that string, and the test uses that shape
    (a raw list would let a broken decode pass silently)."""
    from langchain_core.messages import ToolMessage

    wrapped = ToolMessage(
        content=json.dumps([{"id": "a"}, {"id": "b"}]),
        tool_call_id="call_1",
    )
    agent = FakeAgent([
        _tool("search_entities", {"query": "checkpoint"}),
        _tool_end("search_entities", {"query": "checkpoint"}, output=wrapped),
        _tok("done"),
    ])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert {"type": "tool_result", "name": "search_entities",
            "input": {"query": "checkpoint"}, "count": 2, "unit": "matches"} in events


def test_chat_no_thinking_emits_no_thinking_events():
    """Models that don't expose thinking (str content) produce no thinking
    events — the stream is exactly as before."""
    agent = FakeAgent([_tok("Plain answer.")])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert not [e for e in events if e["type"] == "thinking"]


def test_chat_emits_source_event_and_done_sources():
    read_args = {
        "repo": "timeplus-knowledge",
        "file_path": "src/tpk/server.py",
        "line_start": 10,
        "line_end": 20,
    }
    agent = FakeAgent(
        [
            _tool("read_source", read_args),
            _tool_end("read_source", read_args, output="some code"),
            _tok("Hello"),
        ]
    )
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)

    expected_source = {
        "type": "source",
        "n": 1,
        "repo": "timeplus-knowledge",
        "file_path": "src/tpk/server.py",
        "line_start": 10,
        "line_end": 20,
        "kind": "file",
    }
    assert expected_source in events
    assert events[-1] == {"type": "done", "text": "Hello", "sources": [expected_source]}


def test_chat_dedups_repeated_source_reads():
    read_args = {
        "repo": "timeplus-knowledge",
        "file_path": "src/tpk/server.py",
        "line_start": 10,
        "line_end": 20,
    }
    other_args = {
        "repo": "timeplus-knowledge",
        "file_path": "src/tpk/agent.py",
        "line_start": 1,
        "line_end": 5,
    }
    agent = FakeAgent(
        [
            _tool_end("read_source", read_args),
            _tool_end("read_source", read_args),  # same (repo, file_path, line_start) -> reuse n=1
            _tool_end("read_source", other_args),  # new location -> n=2
            _tok("Hello"),
        ]
    )
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)

    source_events = [e for e in events if e["type"] == "source"]
    assert [e["n"] for e in source_events] == [1, 2]
    assert events[-1]["sources"] == source_events


def test_chat_ignores_malformed_tool_end_event():
    """A read_source on_tool_end missing its input args must be skipped, not
    break the stream."""
    agent = FakeAgent(
        [
            {"event": "on_tool_end", "name": "read_source", "data": {"output": "x"}},
            _tok("Hello"),
        ]
    )
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert not any(e["type"] == "source" for e in events)
    assert events[-1] == {"type": "done", "text": "Hello", "sources": []}


def test_chat_ignores_on_tool_end_for_other_tools():
    agent = FakeAgent(
        [
            _tool_end("search_entities", {"query": "q"}, output="[]"),
            _tok("Hello"),
        ]
    )
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert not any(e["type"] == "source" for e in events)
    assert events[-1] == {"type": "done", "text": "Hello", "sources": []}


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


# -- chat audit (support history, #45) --------------------------------------


def test_chat_audit_logs_successful_turn():
    read_args = {
        "repo": "timeplus-knowledge",
        "file_path": "src/tpk/server.py",
        "line_start": 10,
        "line_end": 20,
    }
    agent = FakeAgent(
        [
            _tool_end("search_entities", {"query": "q"}, output='[{"id": "a"}, {"id": "b"}]'),
            _tool("read_source", read_args),
            _tool_end("read_source", read_args, output="some code"),
            _tok("Hello "),
            _tok("world"),
        ]
    )
    records = []
    client = TestClient(
        create_app(agent=agent, auth=_StubAuth(), audit_sink=records.append)
    )
    resp = client.post("/chat", json={"message": "how does chat work?"})
    assert resp.status_code == 200

    assert len(records) == 1
    rec = records[0]
    assert rec.status == "ok"
    assert rec.error == ""
    assert rec.username == "tester"
    assert rec.question == "how does chat work?"
    assert rec.answer == "Hello world"
    assert rec.history_len == 0
    # Both tool calls captured; search_entities carries its match count.
    assert [t["name"] for t in rec.tool_calls] == ["search_entities", "read_source"]
    assert rec.tool_calls[0]["count"] == 2 and rec.tool_calls[0]["unit"] == "matches"
    # The cited source is captured too.
    assert len(rec.sources) == 1
    assert rec.sources[0]["file_path"] == "src/tpk/server.py"
    # Row serializes to exactly the schema's columns.
    from tpk.audit import CHAT_AUDIT_COLUMNS

    assert len(rec.to_row()) == len(CHAT_AUDIT_COLUMNS)


def test_chat_audit_logs_error_turn():
    class BoomAgent:
        async def astream_events(self, _input, version="v2", config=None):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    records = []
    client = TestClient(
        create_app(agent=BoomAgent(), auth=_StubAuth(), audit_sink=records.append)
    )
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)

    # Client still gets the error event...
    assert events[-1]["type"] == "error"
    # ...and the failed turn is recorded.
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].error == "RuntimeError"
    assert records[0].question == "hi"


def test_chat_audit_failure_does_not_break_stream():
    def boom_sink(_record):
        raise RuntimeError("audit store down")

    agent = FakeAgent([_tok("Hello")])
    client = TestClient(
        create_app(agent=agent, auth=_StubAuth(), audit_sink=boom_sink)
    )
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1] == {"type": "done", "text": "Hello", "sources": []}


def test_chat_audit_disabled_writes_nothing():
    # No sink injected and TPK_CHAT_AUDIT=0 (conftest) -> no audit sink built.
    agent = FakeAgent([_tok("Hello")])
    client = TestClient(create_app(agent=agent, auth=_StubAuth()))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert _parse_sse(resp.text)[-1]["text"] == "Hello"


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
    assert events[-1] == {"type": "done", "text": "The grounded final answer.", "sources": []}


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

    from tpk.auth import CAP_CHAT
    stub = _StubAuth(user=User("viewer1", "", "viewer"), caps={CAP_CHAT})
    client = TestClient(create_app(agent=FakeAgent([_tok("hi")]), auth=stub))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert ROLE_SCOPE.get() is None


# -- thinking suppression without source:view capability (Task 3) --


def _think_agent():
    # One thinking delta, then an answer token.
    return FakeAgent([_think("secret internal reasoning"), _tok("the answer")])


def _nonadmin_auth_with_role(monkeypatch, caps):
    from tpk import auth as auth_mod
    role = auth_mod.Role("r", [], capabilities=list(caps))
    monkeypatch.setattr(auth_mod, "get_role", lambda *a, **k: role)
    a = _StubAuth(User("u", "", "r"))          # non-admin user
    a._client = lambda: None                    # reach patched get_role, don't raise
    return a


def test_chat_suppresses_thinking_without_source_view(monkeypatch):
    from tpk.auth import CAP_CHAT
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT])  # no source:view
    client = TestClient(create_app(agent=_think_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert not any(e["type"] == "thinking" for e in events)
    # The answer itself still streams.
    assert any(e["type"] == "token" for e in events)


def test_chat_emits_thinking_with_source_view(monkeypatch):
    from tpk.auth import CAP_CHAT, CAP_SOURCE_VIEW
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT, CAP_SOURCE_VIEW])
    client = TestClient(create_app(agent=_think_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert [e["text"] for e in events if e["type"] == "thinking"] == ["secret internal reasoning"]


def test_chat_admin_still_emits_thinking():
    # Default _StubAuth user is admin -> holds every capability.
    client = TestClient(create_app(agent=_think_agent(), auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert any(e["type"] == "thinking" for e in events)


def _source_agent():
    # A read_source tool call (server derives a `source` citation from it),
    # then an answer token.
    args = {"repo": "docs@main", "file_path": "a.md", "line_start": 1, "line_end": 5}
    return FakeAgent([_tool("read_source", args), _tool_end("read_source", args), _tok("the answer")])


def test_chat_withholds_source_and_tool_events_without_source_view(monkeypatch):
    """Without source:view the API stream carries only the answer — no tool,
    tool_result, or source events, and the done event's sources list is empty.
    Client-side hiding is not enough; the reference must not cross the wire."""
    from tpk.auth import CAP_CHAT
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT])  # no source:view
    client = TestClient(create_app(agent=_source_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    types = {e["type"] for e in events}
    assert "source" not in types
    assert "tool" not in types
    assert "tool_result" not in types
    done = next(e for e in events if e["type"] == "done")
    assert done["sources"] == []
    # The answer itself still streams.
    assert any(e["type"] == "token" for e in events)


def test_chat_includes_source_and_tool_events_with_source_view(monkeypatch):
    from tpk.auth import CAP_CHAT, CAP_SOURCE_VIEW
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT, CAP_SOURCE_VIEW])
    client = TestClient(create_app(agent=_source_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    types = {e["type"] for e in events}
    assert "tool" in types and "source" in types
    done = next(e for e in events if e["type"] == "done")
    assert len(done["sources"]) == 1


def test_chat_admin_gets_source_and_tool_events():
    # Default _StubAuth user is admin -> holds every capability.
    client = TestClient(create_app(agent=_source_agent(), auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    types = {e["type"] for e in events}
    assert "tool" in types and "source" in types
    done = next(e for e in events if e["type"] == "done")
    assert len(done["sources"]) == 1
