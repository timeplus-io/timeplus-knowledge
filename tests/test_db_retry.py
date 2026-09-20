"""Unit tests for db.connect_with_retry — infra-free (no timeplusd)."""

import pytest

from tpk import db


def test_connect_with_retry_returns_on_first_success(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "get_client", lambda s: calls.append(s) or "CLIENT")
    result = db.connect_with_retry(object(), timeout_s=10, interval_s=0)
    assert result == "CLIENT"
    assert len(calls) == 1  # no retry when the first attempt works


def test_connect_with_retry_recovers_after_transient_failures(monkeypatch):
    attempts = {"n": 0}

    def flaky(_settings):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("connection refused")
        return "CLIENT"

    monkeypatch.setattr(db, "get_client", flaky)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)  # don't actually wait
    result = db.connect_with_retry(object(), timeout_s=10, interval_s=0.01)
    assert result == "CLIENT"
    assert attempts["n"] == 3  # failed twice, succeeded on the third


def test_connect_with_retry_raises_after_deadline(monkeypatch):
    attempts = {"n": 0}

    def always_fail(_settings):
        attempts["n"] += 1
        raise ConnectionError("connection refused")

    monkeypatch.setattr(db, "get_client", always_fail)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    # Fake clock that jumps well past the deadline after the first attempt, so
    # the retry loop gives up deterministically without real waiting.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1000.0
        return clock["t"]

    monkeypatch.setattr(db.time, "monotonic", fake_monotonic)
    with pytest.raises(ConnectionError):
        db.connect_with_retry(object(), timeout_s=60, interval_s=0.01)
    assert attempts["n"] == 1  # tried once, then the deadline tripped


# -- proxy bypass for a local DB (#77) ---------------------------------------
# A shell-wide HTTP_PROXY makes timeplus_connect route http://localhost:8123
# through the proxy (-> 502 / "Connection closed" in an MCP client). Handing
# the driver a plain pool manager skips its env-proxy lookup.

def _captured_get_client(monkeypatch):
    from tpk import db

    seen = {}
    monkeypatch.setattr(db.timeplus_connect, "get_client",
                        lambda **kw: seen.update(kw) or object())
    return db, seen


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LOCALHOST"])
def test_get_client_bypasses_env_proxy_for_loopback_hosts(monkeypatch, host):
    from tpk.config import Settings

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    db, seen = _captured_get_client(monkeypatch)
    db.get_client(Settings(host=host, user="u", password="p"))
    assert seen["pool_mgr"] is not None


def test_get_client_leaves_remote_hosts_to_the_environment(monkeypatch):
    from tpk.config import Settings

    db, seen = _captured_get_client(monkeypatch)
    db.get_client(Settings(host="timeplusd.internal", user="u", password="p"))
    assert "pool_mgr" not in seen
