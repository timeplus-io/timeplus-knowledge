import time
from datetime import timedelta

import pytest

from conftest import requires_timeplus
from tpk import auth, db

pytestmark = requires_timeplus


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def test_create_returns_plaintext_once_and_stores_only_hash(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    assert token.startswith("tpk_") and rec.hint == token[-4:]
    assert rec.expires_at is None and rec.last_used_at is None
    rows = _eventually(lambda: client.query(
        f"SELECT token_hash FROM {db.latest(db.qualified('kg_api_tokens', prefix))}").result_rows)
    assert rows and rows[0][0] != token and token not in str(rows)


def test_resolve_valid_unknown_and_non_prefixed(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    got = _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    assert got.token_id == rec.token_id and got.username == "alice"
    assert auth.resolve_api_token(client, "tpk_nope", prefix=prefix) is None
    assert auth.resolve_api_token(client, "not-prefixed", prefix=prefix) is None


def test_resolve_bumps_last_used_throttled(tp):
    client, prefix = tp
    token, _ = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    first = _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix)[0].last_used_at)
    assert first is not None
    auth.resolve_api_token(client, token, prefix=prefix)  # within 5 min: no rewrite
    time.sleep(0.5)
    assert auth.list_api_tokens(client, "alice", prefix=prefix)[0].last_used_at == first


def test_expiry(tp, monkeypatch):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "ci", expires_days=30, prefix=prefix)
    assert rec.expires_at is not None
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    real_now = auth._now
    monkeypatch.setattr(auth, "_now", lambda: real_now() + timedelta(days=31))
    assert auth.resolve_api_token(client, token, prefix=prefix) is None
    assert auth.list_api_tokens(client, "alice", prefix=prefix) == []


def test_validation(tp):
    client, prefix = tp
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "", prefix=prefix)
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "x" * 65, prefix=prefix)
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "ok", expires_days=7, prefix=prefix)


def test_revoke_is_owner_scoped(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix))
    assert auth.revoke_api_token(client, "bob", rec.token_id, prefix=prefix) is False
    assert auth.revoke_api_token(client, "alice", rec.token_id, prefix=prefix) is True
    assert _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix),
                       predicate=lambda v: v is None) is None


def test_delete_user_tokens_and_cap(tp, monkeypatch):
    client, prefix = tp
    monkeypatch.setattr(auth, "MAX_API_TOKENS_PER_USER", 2)
    auth.create_api_token(client, "alice", "a", prefix=prefix)
    auth.create_api_token(client, "alice", "b", prefix=prefix)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                predicate=lambda v: len(v) == 2)
    with pytest.raises(auth.ApiTokenLimitError):
        auth.create_api_token(client, "alice", "c", prefix=prefix)
    auth.delete_user_api_tokens(client, "alice", prefix=prefix)
    assert _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                       predicate=lambda v: v == []) == []
