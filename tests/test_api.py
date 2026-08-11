import time

from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk.server import create_app

pytestmark = requires_timeplus


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _client(tp, monkeypatch):
    client, prefix = tp
    monkeypatch.delenv("TPK_ADMIN_TOKEN", raising=False)
    import tpk.api as api_mod

    # jobs must not run real graphify: stub ingest to a fast success
    from tpk.ingest import IngestResult

    def fake_ingest(client_, cfg, prefix="", backend=None, model=None,
                    token_budget=0, stream=False, out_root=None, **kw):
        from tpk.config import entry_key
        return IngestResult(entry_key(cfg), "j", 3, 2, "ok")

    monkeypatch.setattr(api_mod, "ingest_repo", fake_ingest)
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix)), prefix


def test_add_list_toggle_delete_flow(tp, monkeypatch):
    c, prefix = _client(tp, monkeypatch)
    r = c.post("/api/repos", json={"name": "demo", "github": "o/demo", "ref": "v1",
                                   "visibility": "internal", "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "demo@v1"

    rows = c.get("/api/repos").json()
    assert [x["entry_key"] for x in rows] == ["demo@v1"]
    assert rows[0]["enabled"] is True

    assert c.post("/api/repos/toggle",
                  json={"name": "demo", "ref": "v1", "enabled": False}).json()["ok"]
    assert c.get("/api/repos").json()[0]["enabled"] is False

    assert c.post("/api/repos/delete", json={"name": "demo", "ref": "v1"}).json()["ok"]
    assert c.get("/api/repos").json() == []
    assert c.post("/api/repos/toggle",
                  json={"name": "demo", "ref": "v1", "enabled": True}).status_code == 404


def test_add_validation(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    assert c.post("/api/repos", json={"name": "x"}).status_code == 422           # no source
    assert c.post("/api/repos", json={"name": "x", "github": "o/x"}).status_code == 422  # no ref
    # '@' in name is rejected by the name regex itself (400), not a
    # dedicated '@' check -- the regex already excludes it.
    assert c.post("/api/repos", json={"name": "a@b", "path": "/p"}).status_code == 400


def test_add_rejects_bad_name(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    for name in ["../etc", "a/b", "", "a b", "a$b"]:
        r = c.post("/api/repos", json={"name": name, "github": "o/x", "ref": "v1"})
        assert r.status_code == 400, (name, r.text)


def test_add_rejects_dot_and_dotdot_name(tp, monkeypatch):
    # "." and ".." both match `_NAME_RE` (only [A-Za-z0-9._-]) but, used as
    # a raw filesystem path segment in `resolved_repo_path`
    # (checkout_root() / name / ref), ".." walks the checkout destination
    # outside the checkout cache entirely -- must be rejected explicitly.
    c, _ = _client(tp, monkeypatch)
    for name in [".", ".."]:
        r = c.post("/api/repos", json={"name": name, "github": "o/x", "ref": "v1"})
        assert r.status_code == 400, (name, r.text)


def test_add_rejects_bad_ref(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/repos", json={"name": "x", "github": "o/x", "ref": "-evil"})
    assert r.status_code == 400
    r = c.post("/api/repos", json={"name": "x", "github": "o/x", "ref": "a/../b"})
    assert r.status_code == 400
    r = c.post("/api/repos", json={"name": "x", "github": "o/x", "ref": "a b"})
    assert r.status_code == 400


def test_add_accepts_ref_with_slash(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/repos", json={"name": "x", "github": "o/x", "ref": "release/1.0",
                                   "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "x@release/1.0"


def test_path_entry_requires_admin_token(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    monkeypatch.delenv("TPK_ADMIN_TOKEN", raising=False)
    r = c.post("/api/repos", json={"name": "local", "path": "/tmp/somewhere",
                                   "ingest": False})
    assert r.status_code == 403

    monkeypatch.setenv("TPK_ADMIN_TOKEN", "sekret")
    r = c.post("/api/repos", json={"name": "local", "path": "/tmp/somewhere",
                                   "ingest": False},
               headers={"X-Admin-Token": "sekret"})
    assert r.status_code == 200 and r.json()["entry_key"] == "local"


def test_reindex_job_runs_to_ok(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    c.post("/api/repos", json={"name": "demo", "github": "o/demo", "ref": "v1",
                               "ingest": False})
    job_id = c.post("/api/repos/reindex", json={"name": "demo", "ref": "v1"}).json()["job_id"]
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = c.get(f"/api/jobs/{job_id}").json()["status"]
        if status in ("ok", "failed"):
            break
        time.sleep(0.1)
    assert status == "ok"
    jobs = c.get("/api/jobs").json()
    assert jobs[0]["entry_key"] == "demo@v1" and jobs[0]["nodes"] == 3


def test_admin_token_gate(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    monkeypatch.setenv("TPK_ADMIN_TOKEN", "sekret")
    assert c.get("/api/repos").status_code == 401
    assert c.get("/api/repos", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/api/repos", headers={"X-Admin-Token": "sekret"}).status_code == 200
