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
