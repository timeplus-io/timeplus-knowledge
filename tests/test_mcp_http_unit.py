"""Infra-free unit tests for the /mcp ASGI wrapper (#74).

The end-to-end matrix lives in tests/test_mcp_http.py (needs a live store);
these cover the transport-security config and the non-HTTP scope guard.
"""

import asyncio

from tpk.mcp_http import McpAuth, _transport_security


def test_transport_security_off_by_default(monkeypatch):
    monkeypatch.delenv("TPK_MCP_ALLOWED_HOSTS", raising=False)
    assert _transport_security().enable_dns_rebinding_protection is False


def test_transport_security_allow_list(monkeypatch):
    monkeypatch.setenv("TPK_MCP_ALLOWED_HOSTS", "a.example.com, b.example.com:8000")
    settings = _transport_security()
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["a.example.com", "b.example.com:8000"]


def _run(scope):
    """Drive McpAuth with a never-authenticated inner app; returns (inner
    calls, messages sent)."""
    calls, sent = [], []

    async def inner(scope, receive, send):
        calls.append(scope)

    async def send(message):
        sent.append(message)

    asyncio.run(McpAuth(inner, auth=None)(scope, None, send))
    return calls, sent


def test_websocket_scope_is_closed_not_passed_through():
    calls, sent = _run({"type": "websocket", "headers": []})
    assert calls == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_unknown_scope_type_is_dropped():
    calls, sent = _run({"type": "something-else", "headers": []})
    assert calls == [] and sent == []
