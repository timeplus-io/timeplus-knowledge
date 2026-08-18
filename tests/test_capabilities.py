"""Role-based function capabilities (issue #23): capability model, endpoint
enforcement, effective-capability reporting, and bounded delegation."""

import time

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth
from tpk.auth import (
    ALL_CAPABILITIES, CAP_CHAT, CAP_CORPUS_MANAGE, CAP_CORPUS_VIEW,
    CAP_EXPLORE, CAP_USERS_MANAGE, CAP_USERS_VIEW, Role, User,
    effective_capabilities, expand_capabilities,
)
from tpk.server import create_app


# -- unit: capability model (no store) -------------------------------------

def test_expand_capabilities_manage_implies_view():
    assert expand_capabilities([CAP_CORPUS_MANAGE]) == {CAP_CORPUS_MANAGE, CAP_CORPUS_VIEW}
    assert expand_capabilities([CAP_USERS_MANAGE]) == {CAP_USERS_MANAGE, CAP_USERS_VIEW}
    assert expand_capabilities([CAP_CHAT]) == {CAP_CHAT}


def test_expand_capabilities_drops_unknowns():
    assert expand_capabilities([CAP_CHAT, "bogus", "corpus:delete"]) == {CAP_CHAT}


def test_parse_capabilities_migration_default():
    # Legacy rows (empty cell) migrate to chat-only; explicit [] stays empty.
    assert auth._parse_capabilities("") == [CAP_CHAT]
    assert auth._parse_capabilities(None) == [CAP_CHAT]
    assert auth._parse_capabilities("[]") == []
    assert auth._parse_capabilities('["chat","explore"]') == [CAP_CHAT, CAP_EXPLORE]
    assert auth._parse_capabilities("not-json") == [CAP_CHAT]


def test_effective_capabilities_admin_and_scoped():
    admin = User("a", "", auth.ROLE_ADMIN)
    assert effective_capabilities(admin, None) == set(ALL_CAPABILITIES)
    scoped = User("u", "", "power")
    role = Role("power", [], "", [CAP_CORPUS_MANAGE])
    assert effective_capabilities(scoped, role) == {CAP_CORPUS_MANAGE, CAP_CORPUS_VIEW}
    # Missing role fails closed to no capabilities.
    assert effective_capabilities(scoped, None) == set()


# -- integration fixtures --------------------------------------------------

pytestmark = requires_timeplus


def _eventually(fn, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


@pytest.fixture()
def client(tp):
    return tp[0]


@pytest.fixture()
def prefix(tp):
    return tp[1]


@pytest.fixture()
def c(prefix):
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))


def _login_token(c, username, password):
    _eventually(lambda: c.post("/auth/login", json={"username": username, "password": password}).status_code == 200)
    return c.post("/auth/login", json={"username": username, "password": password}).json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_user(client, prefix, username, role, password="password-1"):
    auth.upsert_user(client, User(username, auth.hash_password(password), role), prefix=prefix)


def _mk_role(client, prefix, name, caps, entry_keys=None):
    auth.upsert_role(client, Role(name, entry_keys or [], "", list(caps)), prefix=prefix)


# -- /auth/me + login report effective capabilities ------------------------

def test_me_reports_effective_capabilities(c, client, prefix):
    _mk_role(client, prefix, "power", [CAP_CHAT, CAP_EXPLORE])
    _mk_user(client, prefix, "pat", "power")
    token = _login_token(c, "pat", "password-1")
    me = c.get("/auth/me", headers=_hdr(token)).json()
    assert me["capabilities"] == sorted([CAP_CHAT, CAP_EXPLORE])


def test_me_admin_has_all_capabilities(c, client, prefix):
    _mk_user(client, prefix, "root", auth.ROLE_ADMIN, password="adminpass1")
    token = _login_token(c, "root", "adminpass1")
    me = c.get("/auth/me", headers=_hdr(token)).json()
    assert me["capabilities"] == sorted(ALL_CAPABILITIES)


def test_login_includes_capabilities_with_manage_implying_view(c, client, prefix):
    _mk_role(client, prefix, "curator", [CAP_CORPUS_MANAGE])
    _mk_user(client, prefix, "cara", "curator")
    _eventually(lambda: c.post("/auth/login", json={"username": "cara", "password": "password-1"}).status_code == 200)
    body = c.post("/auth/login", json={"username": "cara", "password": "password-1"}).json()
    assert set(body["capabilities"]) == {CAP_CORPUS_MANAGE, CAP_CORPUS_VIEW}


# -- endpoint enforcement --------------------------------------------------

def test_corpus_view_vs_manage(c, client, prefix):
    _mk_role(client, prefix, "corp-view", [CAP_CORPUS_VIEW])
    _mk_user(client, prefix, "vic", "corp-view")
    tok = _login_token(c, "vic", "password-1")
    assert c.get("/api/repos", headers=_hdr(tok)).status_code == 200
    # Mutation needs corpus:manage -> 403 (not 404), proving the gate fires
    # before the handler.
    r = c.post("/api/repos/toggle", json={"name": "nope", "ref": "v1", "enabled": True}, headers=_hdr(tok))
    assert r.status_code == 403


def test_corpus_manage_reaches_handler(c, client, prefix):
    _mk_role(client, prefix, "corp-mgr", [CAP_CORPUS_MANAGE])
    _mk_user(client, prefix, "mel", "corp-mgr")
    tok = _login_token(c, "mel", "password-1")
    # manage implies view -> list works
    assert c.get("/api/repos", headers=_hdr(tok)).status_code == 200
    # toggle passes the gate and reaches the handler -> 404 for a missing entry
    r = c.post("/api/repos/toggle", json={"name": "nope", "ref": "v1", "enabled": True}, headers=_hdr(tok))
    assert r.status_code == 404


def test_users_view_vs_manage(c, client, prefix):
    _mk_role(client, prefix, "user-view", [CAP_USERS_VIEW])
    _mk_user(client, prefix, "uma", "user-view")
    tok = _login_token(c, "uma", "password-1")
    assert c.get("/api/users", headers=_hdr(tok)).status_code == 200
    assert c.get("/api/roles", headers=_hdr(tok)).status_code == 200
    r = c.post("/api/users", json={"username": "new", "password": "password-2", "role": "user-view"},
               headers=_hdr(tok))
    assert r.status_code == 403


def test_no_capability_is_forbidden_everywhere(c, client, prefix):
    _mk_role(client, prefix, "empty", [])
    _mk_user(client, prefix, "eve", "empty")
    tok = _login_token(c, "eve", "password-1")
    assert c.get("/api/repos", headers=_hdr(tok)).status_code == 403
    assert c.get("/api/users", headers=_hdr(tok)).status_code == 403


# -- role upsert capability validation -------------------------------------

def test_upsert_role_rejects_unknown_capability(c, client, prefix):
    _mk_user(client, prefix, "root", auth.ROLE_ADMIN, password="adminpass1")
    tok = _login_token(c, "root", "adminpass1")
    r = c.post("/api/roles", json={"name": "r1", "entry_keys": [], "capabilities": ["chat", "bogus"]},
               headers=_hdr(tok))
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]


def test_upsert_role_roundtrips_capabilities(c, client, prefix):
    _mk_user(client, prefix, "root", auth.ROLE_ADMIN, password="adminpass1")
    tok = _login_token(c, "root", "adminpass1")
    c.post("/api/roles", json={"name": "r2", "entry_keys": [], "capabilities": [CAP_CHAT, CAP_EXPLORE]},
           headers=_hdr(tok))
    roles = _eventually(lambda: [r for r in c.get("/api/roles", headers=_hdr(tok)).json() if r["name"] == "r2"])
    assert roles and set(roles[0]["capabilities"]) == {CAP_CHAT, CAP_EXPLORE}


# -- bounded delegation ----------------------------------------------------

@pytest.fixture()
def manager(c, client, prefix):
    """A non-admin manager: chat+explore+users:manage, corpus scope alpha@v1."""
    _mk_role(client, prefix, "manager", [CAP_CHAT, CAP_EXPLORE, CAP_USERS_MANAGE],
             entry_keys=["alpha@v1"])
    _mk_user(client, prefix, "mgr", "manager")
    return _login_token(c, "mgr", "password-1")


def test_manager_can_grant_within_own_scope(c, manager):
    r = c.post("/api/roles", json={"name": "sub", "entry_keys": ["alpha@v1"],
                                   "capabilities": [CAP_CHAT, CAP_EXPLORE]}, headers=_hdr(manager))
    assert r.status_code == 200


def test_manager_cannot_grant_capability_beyond_own(c, manager):
    r = c.post("/api/roles", json={"name": "sub2", "entry_keys": [],
                                   "capabilities": [CAP_CORPUS_MANAGE]}, headers=_hdr(manager))
    assert r.status_code == 403


def test_manager_cannot_grant_corpus_beyond_own(c, manager):
    r = c.post("/api/roles", json={"name": "sub3", "entry_keys": ["beta@v1"],
                                   "capabilities": [CAP_CHAT]}, headers=_hdr(manager))
    assert r.status_code == 403


def test_manager_cannot_set_token_budget(c, client, prefix, manager):
    # A non-admin manager may create/manage users and roles, but not set or
    # change a daily token budget (admin-only cost lever, #62).
    r = c.post("/api/roles", json={"name": "sub-budget", "entry_keys": ["alpha@v1"],
                                   "capabilities": [CAP_CHAT], "daily_token_limit": 999999},
               headers=_hdr(manager))
    assert r.status_code == 403
    r = c.post("/api/users", json={"username": "richuser", "password": "password-2",
                                   "role": "manager", "daily_token_limit": 999999},
               headers=_hdr(manager))
    assert r.status_code == 403
    # ...but managing without touching the budget (limit 0 = inherit) is fine.
    assert c.post("/api/roles", json={"name": "sub-ok", "entry_keys": ["alpha@v1"],
                                      "capabilities": [CAP_CHAT]},
                  headers=_hdr(manager)).status_code == 200


def test_manager_cannot_assign_admin_role(c, client, prefix, manager):
    r = c.post("/api/users", json={"username": "eviladmin", "password": "password-2",
                                   "role": auth.ROLE_ADMIN}, headers=_hdr(manager))
    assert r.status_code == 403


def test_manager_cannot_manage_admin_users(c, client, prefix, manager):
    _mk_user(client, prefix, "root", auth.ROLE_ADMIN, password="adminpass1")
    _eventually(lambda: auth.get_user(client, "root", prefix=prefix) is not None)
    assert c.post("/api/users/update", json={"username": "root", "disabled": True},
                  headers=_hdr(manager)).status_code == 403
    assert c.post("/api/users/delete", json={"username": "root"},
                  headers=_hdr(manager)).status_code == 403


def test_manager_cannot_overwrite_role_beyond_own(c, client, prefix, manager):
    # A pre-existing role more privileged than the manager may not be
    # overwritten/neutered, even with within-bound new values.
    _mk_role(client, prefix, "senior", [CAP_CORPUS_MANAGE, CAP_USERS_MANAGE])
    _eventually(lambda: auth.get_role(client, "senior", prefix=prefix) is not None)
    r = c.post("/api/roles", json={"name": "senior", "entry_keys": [],
                                   "capabilities": [CAP_CHAT]}, headers=_hdr(manager))
    assert r.status_code == 403


def test_manager_cannot_delete_role_beyond_own(c, client, prefix, manager):
    _mk_role(client, prefix, "bigrole", [CAP_CORPUS_MANAGE])
    _eventually(lambda: auth.get_role(client, "bigrole", prefix=prefix) is not None)
    assert c.post("/api/roles/delete", json={"name": "bigrole"},
                  headers=_hdr(manager)).status_code == 403


def test_admin_is_unbounded(c, client, prefix):
    _mk_user(client, prefix, "root", auth.ROLE_ADMIN, password="adminpass1")
    tok = _login_token(c, "root", "adminpass1")
    r = c.post("/api/roles", json={"name": "anything", "entry_keys": ["beta@v1"],
                                   "capabilities": list(ALL_CAPABILITIES)}, headers=_hdr(tok))
    assert r.status_code == 200
