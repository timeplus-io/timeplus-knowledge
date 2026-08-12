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


def _client(tp, monkeypatch):
    client, prefix = tp
    import tpk.api as api_mod

    # jobs must not run real graphify: stub ingest to a fast success
    from tpk.ingest import IngestResult

    def fake_ingest(client_, cfg, prefix="", backend=None, model=None,
                    token_budget=0, stream=False, out_root=None, **kw):
        from tpk.config import entry_key
        return IngestResult(entry_key(cfg), "j", 3, 2, "ok")

    monkeypatch.setattr(api_mod, "ingest_repo", fake_ingest)
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix)), prefix


@pytest.fixture()
def admin_hdr(tp, monkeypatch):
    client, prefix = tp
    c, _ = _client(tp, monkeypatch)
    auth.upsert_user(client, auth.User("root", auth.hash_password("adminpass1"), auth.ROLE_ADMIN), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_add_list_toggle_delete_flow(tp, monkeypatch, admin_hdr):
    c, prefix = _client(tp, monkeypatch)
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "demo", "github": "o/demo", "ref": "v1",
                     "visibility": "internal", "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "demo@v1"

    rows = c.get("/api/repos", headers=admin_hdr).json()
    assert [x["entry_key"] for x in rows] == ["demo@v1"]
    assert rows[0]["enabled"] is True

    assert c.post("/api/repos/toggle", headers=admin_hdr,
                  json={"name": "demo", "ref": "v1", "enabled": False}).json()["ok"]
    assert c.get("/api/repos", headers=admin_hdr).json()[0]["enabled"] is False

    assert c.post("/api/repos/delete", headers=admin_hdr,
                  json={"name": "demo", "ref": "v1"}).json()["ok"]
    assert c.get("/api/repos", headers=admin_hdr).json() == []
    assert c.post("/api/repos/toggle", headers=admin_hdr,
                  json={"name": "demo", "ref": "v1", "enabled": True}).status_code == 404


def test_add_validation(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    assert c.post("/api/repos", headers=admin_hdr, json={"name": "x"}).status_code == 422  # no source
    assert c.post("/api/repos", headers=admin_hdr,
                  json={"name": "x", "github": "o/x"}).status_code == 422  # no ref
    # '@' in name is rejected by the name regex itself (400), not a
    # dedicated '@' check -- the regex already excludes it.
    assert c.post("/api/repos", headers=admin_hdr,
                  json={"name": "a@b", "path": "/p"}).status_code == 400


def test_add_rejects_bad_name(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    for name in ["../etc", "a/b", "", "a b", "a$b"]:
        r = c.post("/api/repos", headers=admin_hdr,
                   json={"name": name, "github": "o/x", "ref": "v1"})
        assert r.status_code == 400, (name, r.text)


def test_add_rejects_dot_and_dotdot_name(tp, monkeypatch, admin_hdr):
    # "." and ".." both match `_NAME_RE` (only [A-Za-z0-9._-]) but, used as
    # a raw filesystem path segment in `resolved_repo_path`
    # (checkout_root() / name / ref), ".." walks the checkout destination
    # outside the checkout cache entirely -- must be rejected explicitly.
    c, _ = _client(tp, monkeypatch)
    for name in [".", ".."]:
        r = c.post("/api/repos", headers=admin_hdr,
                   json={"name": name, "github": "o/x", "ref": "v1"})
        assert r.status_code == 400, (name, r.text)


def test_add_rejects_bad_ref(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "x", "github": "o/x", "ref": "-evil"})
    assert r.status_code == 400
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "x", "github": "o/x", "ref": "a/../b"})
    assert r.status_code == 400
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "x", "github": "o/x", "ref": "a b"})
    assert r.status_code == 400


def test_add_accepts_ref_with_slash(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "x", "github": "o/x", "ref": "release/1.0",
                     "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "x@release/1.0"


def test_path_entry_succeeds_with_admin(tp, monkeypatch, admin_hdr):
    # Path-type entries require admin auth like every other /api route --
    # there is no separate TPK_ADMIN_TOKEN gate anymore.
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/repos", headers=admin_hdr,
               json={"name": "local", "path": "/tmp/somewhere", "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "local"


def test_reindex_job_runs_to_ok(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    c.post("/api/repos", headers=admin_hdr,
           json={"name": "demo", "github": "o/demo", "ref": "v1", "ingest": False})
    job_id = c.post("/api/repos/reindex", headers=admin_hdr,
                    json={"name": "demo", "ref": "v1"}).json()["job_id"]
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = c.get(f"/api/jobs/{job_id}", headers=admin_hdr).json()["status"]
        if status in ("ok", "failed"):
            break
        time.sleep(0.1)
    assert status == "ok"
    jobs = c.get("/api/jobs", headers=admin_hdr).json()
    assert jobs[0]["entry_key"] == "demo@v1" and jobs[0]["nodes"] == 3


def test_api_requires_admin(tp, monkeypatch, admin_hdr):
    client, prefix = tp
    c, _ = _client(tp, monkeypatch)
    auth.upsert_role(client, auth.Role("viewer", []), prefix=prefix)
    auth.upsert_user(client, auth.User("eve", auth.hash_password("password-1"), "viewer"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "eve", "password": "password-1"}).status_code == 200)
    t = c.post("/auth/login", json={"username": "eve", "password": "password-1"}).json()["token"]
    for method, path in [("get", "/api/repos"), ("get", "/api/jobs"), ("get", "/api/users"), ("get", "/api/roles")]:
        assert getattr(c, method)(path).status_code == 401  # no token
        assert getattr(c, method)(path, headers={"Authorization": f"Bearer {t}"}).status_code == 403  # non-admin


def test_user_crud_via_api(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/users", headers=admin_hdr, json={
        "username": "sam", "password": "password-9", "role": "viewer"})
    assert r.status_code == 400  # role does not exist yet
    assert c.post("/api/roles", headers=admin_hdr, json={
        "name": "viewer", "entry_keys": ["docs@main"], "description": ""}).status_code == 200
    assert c.post("/api/users", headers=admin_hdr, json={
        "username": "sam", "password": "password-9", "role": "viewer"}).status_code == 200
    _eventually(lambda: any(u["username"] == "sam" for u in c.get("/api/users", headers=admin_hdr).json()))
    users = c.get("/api/users", headers=admin_hdr).json()
    sam = next(u for u in users if u["username"] == "sam")
    assert sam["must_change_password"] is True and "password_hash" not in sam
    # disable kills sessions
    _eventually(lambda: c.post("/auth/login", json={"username": "sam", "password": "password-9"}).status_code == 200)
    t = c.post("/auth/login", json={"username": "sam", "password": "password-9"}).json()["token"]
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "sam", "disabled": True}).status_code == 200
    _eventually(lambda: c.get("/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401)


def test_role_reserved_in_use_and_delete(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "admin", "entry_keys": []}).status_code == 400
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "team", "entry_keys": ["docs@main"]}).status_code == 200
    assert c.post("/api/users", headers=admin_hdr, json={
        "username": "tess", "password": "password-9", "role": "team"}).status_code == 200
    _eventually(lambda: c.post("/api/roles/delete", headers=admin_hdr, json={"name": "team"}).status_code == 409)
    assert c.post("/api/users/delete", headers=admin_hdr, json={"username": "tess"}).status_code == 200
    _eventually(lambda: c.post("/api/roles/delete", headers=admin_hdr, json={"name": "team"}).status_code == 200)


def test_last_admin_protection(tp, monkeypatch, admin_hdr):
    c, _ = _client(tp, monkeypatch)
    assert c.post("/api/users/delete", headers=admin_hdr, json={"username": "root"}).status_code == 400
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "root", "disabled": True}).status_code == 400
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "other", "entry_keys": []}).status_code == 200
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "root", "role": "other"}).status_code == 400
