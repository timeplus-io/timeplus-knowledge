"""Anonymous chat over the public corpus (#94): off by default; when on, a
request with NO credentials is the `anonymous` principal -- chat only, scoped
to enabled public entries. A bad credential is never downgraded to anonymous."""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth, corpus
from tpk.config import RepoConfig
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
def env(tp, monkeypatch):
    client, prefix = tp
    corpus.upsert_entry(client, RepoConfig(name="docs", github="o/docs", ref="main", visibility="public"), prefix=prefix)
    corpus.upsert_entry(client, RepoConfig(name="proton", github="o/proton", ref="v1", visibility="internal"), prefix=prefix)
    corpus.upsert_entry(client, RepoConfig(name="old-docs", github="o/docs", ref="v0", visibility="public",
                                           enabled=False), prefix=prefix)
    _eventually(lambda: corpus.list_entries(client, prefix=prefix), lambda v: len(v) == 3)
    return client, prefix


def _app(prefix):
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))


# -- the principal -------------------------------------------------------------

def test_anonymous_principal_has_chat_only():
    caps = auth.effective_capabilities(auth.ANONYMOUS, None)
    assert caps == {auth.CAP_CHAT}
    assert auth.ANONYMOUS.username == auth.ANONYMOUS_USERNAME == "anonymous"
    assert auth.ANONYMOUS.role == auth.ROLE_ANONYMOUS


def test_public_scope_is_the_enabled_public_entries(env):
    client, prefix = env
    assert auth.public_scope(client, prefix=prefix) == frozenset({"docs@main"})   # not proton, not disabled old-docs


def test_public_scope_fails_closed():
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("store down")
    assert auth.public_scope(Boom()) == frozenset()


def test_resolve_scope_for_anonymous_is_the_public_scope(env):
    client, prefix = env
    assert auth.resolve_scope(client, auth.ANONYMOUS, prefix=prefix) == frozenset({"docs@main"})


# -- the gate ------------------------------------------------------------------

def test_off_by_default_everything_stays_401(env):
    client, prefix = env
    c = _app(prefix)
    assert c.get("/auth/me").status_code == 401
    assert c.get("/chat/usage").status_code == 401
    assert c.post("/chat", json={"message": "hi"}).status_code == 401


def test_on_no_credentials_is_anonymous_for_chat_only(env, monkeypatch):
    monkeypatch.setenv("TPK_ANONYMOUS_ACCESS", "1")
    client, prefix = env
    c = _app(prefix)
    me = c.get("/auth/me")
    assert me.status_code == 200
    assert me.json() == {"username": "anonymous", "role": "anonymous", "must_change_password": False,
                         "capabilities": ["chat"], "anonymous": True}
    assert c.get("/chat/usage").status_code == 200
    assert c.get("/chat/model").status_code == 200
    # everything that is not `chat` is still closed
    assert c.get("/api/graph/search", params={"q": "x"}).status_code in (401, 403, 404)
    assert c.get("/api/repos").status_code in (401, 403)
    assert c.get("/api/tokens").status_code in (401, 403)
    assert c.get("/api/users").status_code in (401, 403)
    assert c.post("/auth/logout").status_code == 401
    assert c.post("/auth/change-password", json={"old_password": "a", "new_password": "b"}).status_code == 401


def test_on_a_bad_credential_is_still_401(env, monkeypatch):
    monkeypatch.setenv("TPK_ANONYMOUS_ACCESS", "1")
    client, prefix = env
    c = _app(prefix)
    for hdr in ({"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        assert c.get("/auth/me", headers=hdr).status_code == 401, hdr
        assert c.post("/chat", json={"message": "hi"}, headers=hdr).status_code == 401, hdr


def test_on_a_real_login_still_wins(env, monkeypatch):
    monkeypatch.setenv("TPK_ANONYMOUS_ACCESS", "1")
    client, prefix = env
    c = _app(prefix)
    auth.upsert_user(client, auth.User("root", auth.hash_password("adminpass1"), auth.ROLE_ADMIN), prefix=prefix)
    login = lambda: c.post("/auth/login", json={"username": "root", "password": "adminpass1"})
    _eventually(lambda: login().status_code == 200)
    me = c.get("/auth/me", headers={"Authorization": f"Bearer {login().json()['token']}"}).json()
    assert me["username"] == "root" and me.get("anonymous") is not True


def test_anonymous_username_is_reserved(env, monkeypatch):
    client, prefix = env
    c = _app(prefix)
    auth.upsert_user(client, auth.User("root", auth.hash_password("adminpass1"), auth.ROLE_ADMIN), prefix=prefix)
    login = lambda: c.post("/auth/login", json={"username": "root", "password": "adminpass1"})
    _eventually(lambda: login().status_code == 200)
    hdr = {"Authorization": f"Bearer {login().json()['token']}"}
    r = c.post("/api/users", headers=hdr, json={"username": "anonymous", "password": "password-1", "role": "admin"})
    assert r.status_code == 400 and "reserved" in r.text
    r = c.post("/api/roles", headers=hdr, json={"name": "anonymous", "entry_keys": []})
    assert r.status_code == 400 and "reserved" in r.text
    # and a login as "anonymous" can never succeed, even if a row existed
    assert c.post("/auth/login", json={"username": "anonymous", "password": "x"}).status_code == 401
