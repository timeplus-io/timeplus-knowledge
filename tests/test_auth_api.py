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


def _eventually(fn, timeout=5.0, interval=0.1):
    """Poll fn() until it returns truthy, or return the last value on
    timeout. Mutable-stream writes have a small (~100-300ms) propagation lag
    before being visible to a fresh `table(...)` read; see tests/test_auth.py.
    """
    deadline = time.monotonic() + timeout
    result = fn()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


@pytest.fixture()
def client(tp):
    return tp[0]


@pytest.fixture()
def prefix(tp):
    return tp[1]


@pytest.fixture()
def c(prefix):
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))


def _login(c, username, password):
    return c.post("/auth/login", json={"username": username, "password": password})


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_logout_me(c, client, prefix):
    auth.upsert_user(client, auth.User("alice", auth.hash_password("password-1"), "support"), prefix=prefix)
    _eventually(lambda: _login(c, "alice", "password-1").status_code == 200)
    body = _login(c, "alice", "password-1").json()
    assert body["role"] == "support" and body["must_change_password"] is False
    me = c.get("/auth/me", headers=_hdr(body["token"]))
    assert me.status_code == 200 and me.json()["username"] == "alice"
    assert c.post("/auth/logout", headers=_hdr(body["token"])).status_code == 204
    assert c.get("/auth/me", headers=_hdr(body["token"])).status_code == 401


def test_login_uniform_401(c, client, prefix):
    auth.upsert_user(client, auth.User("bob", auth.hash_password("password-1"), "support", disabled=True), prefix=prefix)
    r1 = _login(c, "nobody", "x")
    _eventually(lambda: _login(c, "bob", "password-1").status_code == 401)
    r2 = _login(c, "bob", "wrongpass")
    r3 = _login(c, "bob", "password-1")  # disabled
    assert r1.status_code == r2.status_code == r3.status_code == 401
    assert r1.json() == r2.json() == r3.json() == {"detail": "invalid credentials"}


def test_bad_bearer_401(c):
    assert c.get("/auth/me").status_code == 401
    assert c.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_must_change_password_gate_and_flow(c, client, prefix):
    assert auth.seed_admin(client, prefix=prefix)
    _eventually(lambda: _login(c, "admin", "changeme").status_code == 200)
    body = _login(c, "admin", "changeme").json()
    assert body["must_change_password"] is True
    t = body["token"]
    # gated endpoint blocked with the distinct code (POST /chat gets
    # Depends(auth.require_user) in this task; /api routes still use the
    # old X-Admin-Token gate until Task 3)
    blocked = c.post("/chat", headers=_hdr(t), json={"message": "hi"})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "password_change_required"
    # policy failures
    assert c.post("/auth/change-password", headers=_hdr(t),
                  json={"old_password": "changeme", "new_password": "short"}).status_code == 400
    assert c.post("/auth/change-password", headers=_hdr(t),
                  json={"old_password": "wrong", "new_password": "long-enough-1"}).status_code == 401
    # success clears the flag; same session keeps working
    ok = c.post("/auth/change-password", headers=_hdr(t),
                json={"old_password": "changeme", "new_password": "long-enough-1"})
    assert ok.status_code == 200
    _eventually(lambda: c.get("/auth/me", headers=_hdr(t)).json()["must_change_password"] is False)


def test_change_password_invalidates_other_sessions(c, client, prefix):
    auth.upsert_user(client, auth.User("carol", auth.hash_password("password-1"), "support"), prefix=prefix)
    _eventually(lambda: _login(c, "carol", "password-1").status_code == 200)
    t1 = _login(c, "carol", "password-1").json()["token"]
    t2 = _login(c, "carol", "password-1").json()["token"]
    assert c.post("/auth/change-password", headers=_hdr(t1),
                  json={"old_password": "password-1", "new_password": "password-2xy"}).status_code == 200
    _eventually(lambda: c.get("/auth/me", headers=_hdr(t2)).status_code == 401)
    assert c.get("/auth/me", headers=_hdr(t1)).status_code == 200


def test_chat_requires_user(c):
    assert c.post("/chat", json={"message": "hi"}).status_code == 401
