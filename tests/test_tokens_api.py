import time

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth
from tpk.server import create_app

pytestmark = requires_timeplus


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


@pytest.fixture()
def env(tp):
    client, prefix = tp
    c = TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))
    auth.upsert_role(client, auth.Role("explorer", ["alpha@v1"], capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_role(client, auth.Role("chatter", [], capabilities=[auth.CAP_CHAT]), prefix=prefix)
    auth.upsert_role(client, auth.Role("manager", ["alpha@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_USERS_MANAGE]), prefix=prefix)
    for name, role in [("alice", "explorer"), ("bob", "explorer"), ("carl", "chatter"),
                       ("mia", "manager"), ("root", auth.ROLE_ADMIN)]:
        auth.upsert_user(client, auth.User(name, auth.hash_password("password-1"), role), prefix=prefix)

    def hdr(name):
        login = lambda: c.post("/auth/login", json={"username": name, "password": "password-1"})
        _eventually(lambda: login().status_code == 200)
        return {"Authorization": f"Bearer {login().json()['token']}"}

    return c, client, prefix, hdr


def _create(c, h, name="laptop", **extra):
    return c.post("/api/tokens", headers=h, json={"name": name, **extra})


def test_create_list_revoke_own(env):
    c, _, _, hdr = env
    h = hdr("alice")
    r = _create(c, h, expires_days=30)
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("tpk_") and body["hint"] == body["token"][-4:]
    assert body["expires_at"] is not None and body["last_used_at"] is None
    listed = _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"])
    assert [t["token_id"] for t in listed] == [body["token_id"]]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    assert c.post("/api/tokens/revoke", headers=h, json={"token_id": body["token_id"]}).status_code == 200
    assert _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"],
                       predicate=lambda v: v == []) == []
    assert c.post("/api/tokens/revoke", headers=h, json={"token_id": "nope"}).status_code == 404


def test_requires_explore_and_valid_input(env):
    c, _, _, hdr = env
    assert c.get("/api/tokens").status_code == 401
    assert _create(c, hdr("carl")).status_code == 403
    h = hdr("alice")
    assert _create(c, h, name="").status_code == 400
    assert _create(c, h, expires_days=7).status_code == 400


def test_other_users_tokens_need_users_manage(env):
    c, _, _, hdr = env
    a, b, m = hdr("alice"), hdr("bob"), hdr("mia")
    tid = _create(c, a).json()["token_id"]
    _eventually(lambda: c.get("/api/tokens", headers=a).json()["tokens"])
    assert c.get("/api/tokens", params={"username": "alice"}, headers=b).status_code == 403
    assert c.post("/api/tokens/revoke", headers=b, json={"token_id": tid, "username": "alice"}).status_code == 403
    assert c.post("/api/tokens/revoke-all", headers=b, json={"username": "alice"}).status_code == 403
    assert [t["token_id"] for t in c.get("/api/tokens", params={"username": "alice"}, headers=m).json()["tokens"]] == [tid]
    assert c.post("/api/tokens/revoke-all", headers=m, json={"username": "alice"}).status_code == 200
    assert _eventually(lambda: c.get("/api/tokens", headers=a).json()["tokens"],
                       predicate=lambda v: v == []) == []
    # a non-admin manager may not touch an admin's tokens
    assert c.post("/api/tokens/revoke-all", headers=m, json={"username": "root"}).status_code == 403


def test_cap_409(env, monkeypatch):
    c, _, _, hdr = env
    monkeypatch.setattr(auth, "MAX_API_TOKENS_PER_USER", 1)
    h = hdr("alice")
    assert _create(c, h).status_code == 200
    _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"])
    assert _create(c, h, name="second").status_code == 409


def test_recreated_username_does_not_inherit_old_tokens(env):
    c, client, prefix, hdr = env
    a, r = hdr("alice"), hdr("root")
    token = _create(c, a).json()["token"]
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    assert c.post("/api/users/delete", headers=r, json={"username": "alice"}).status_code == 200
    assert _eventually(lambda: auth.get_user(client, "alice", prefix=prefix),
                       predicate=lambda v: v is None) is None
    assert c.post("/api/users", headers=r, json={
        "username": "alice", "password": "password-2", "role": "explorer"}).status_code == 200
    assert _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix),
                       predicate=lambda v: v is None) is None


def test_creating_a_user_clears_orphaned_credentials(env):
    # A token/session row that outlived its user (partial delete, direct SQL)
    # would otherwise authenticate as the NEXT account with that username.
    c, client, prefix, hdr = env
    a, r = hdr("alice"), hdr("root")
    token = _create(c, a).json()["token"]
    session = auth.create_session(client, "alice", 3600, prefix=prefix)
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    auth.delete_user(client, "alice", prefix=prefix)  # user row only: tokens orphaned
    assert _eventually(lambda: auth.get_user(client, "alice", prefix=prefix),
                       predicate=lambda v: v is None) is None
    assert c.post("/api/users", headers=r, json={
        "username": "alice", "password": "password-2", "role": "explorer"}).status_code == 200
    assert _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix),
                       predicate=lambda v: v is None) is None
    assert _eventually(lambda: auth.get_session(client, session, prefix=prefix),
                       predicate=lambda v: v is None) is None


def test_disable_and_delete_user_drop_tokens(env):
    c, client, prefix, hdr = env
    a, r = hdr("alice"), hdr("root")
    _create(c, a)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix))
    assert c.post("/api/users/update", headers=r, json={"username": "alice", "disabled": True}).status_code == 200
    assert _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                       predicate=lambda v: v == []) == []
    b = hdr("bob")
    _create(c, b)
    _eventually(lambda: auth.list_api_tokens(client, "bob", prefix=prefix))
    assert c.post("/api/users/delete", headers=r, json={"username": "bob"}).status_code == 200
    assert _eventually(lambda: auth.list_api_tokens(client, "bob", prefix=prefix),
                       predicate=lambda v: v == []) == []
