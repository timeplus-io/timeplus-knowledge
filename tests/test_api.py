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


# -- corpus export / import transport (#102) ---------------------------------

def _seed_graph(client, prefix):
    from datetime import datetime, timezone
    from tpk import corpus
    from tpk.config import RepoConfig
    from tpk.model import EDGE_COLUMNS, NODE_COLUMNS, Edge, Node, edge_rows, node_rows
    now = datetime.now(timezone.utc)
    nodes = [Node(f"n{i}", "demo@v1", "function", f"fn{i}", f"demo::fn{i}", "src/x.cpp",
                  i, i + 5, f"summary {i}", "c0", "public") for i in range(3)]
    edges = [Edge("n0", "n1", "calls", "EXTRACTED", "demo@v1")]
    client.insert(f"{prefix}kg_nodes", node_rows(nodes, now), column_names=NODE_COLUMNS)
    client.insert(f"{prefix}kg_edges", edge_rows(edges, now), column_names=EDGE_COLUMNS)
    corpus.upsert_entry(client, RepoConfig(name="demo", ref="v1", github="org/demo"), prefix=prefix)
    _eventually(lambda: int(client.query(
        f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0]) == 3)


def _transfer_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TPK_TRANSFER_DIR", str(tmp_path / "scratch"))


def test_export_download_is_a_valid_bundle_archive(tp, monkeypatch, admin_hdr, tmp_path):
    import io
    import json
    import tarfile
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    _seed_graph(client, prefix)
    c, _ = _client(tp, monkeypatch)

    r = c.get("/api/corpus/export", headers=admin_hdr)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    assert "tpk-corpus-" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.tar.gz"')
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
        manifest = json.load(tar.extractfile("manifest.json"))
    assert manifest["streams"]["kg_nodes"]["rows"] == 3
    assert manifest["streams"]["kg_edges"]["rows"] == 1
    assert manifest["streams"]["kg_repos"]["rows"] == 1
    assert set(names) == {"manifest.json", *(i["file"] for i in manifest["streams"].values())}
    # scratch dir cleaned once the body was sent
    assert list((tmp_path / "scratch").iterdir()) == []
    assert c.get("/api/corpus/export?format=CSV", headers=admin_hdr).status_code == 422


def test_import_upload_runs_as_job(tp, monkeypatch, admin_hdr, tmp_path):
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    _seed_graph(client, prefix)
    c, _ = _client(tp, monkeypatch)
    archive = c.get("/api/corpus/export", headers=admin_hdr).content

    # wipe the graph, then load it back through the upload endpoint
    client.command(f"DROP STREAM IF EXISTS {prefix}kg_nodes")
    from tpk import db
    db.ensure_schema(client, prefix)
    _eventually(lambda: int(client.query(
        f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0]) == 0)

    r = c.post("/api/corpus/import", headers=admin_hdr, data={"replace": "true"},
               files={"file": ("corpus.tar.gz", archive, "application/gzip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["streams"]["kg_nodes"] == 3
    job_id = body["job_id"]
    rec = _eventually(lambda: (lambda j: j if j["status"] in ("ok", "failed") else None)(
        c.get(f"/api/jobs/{job_id}", headers=admin_hdr).json()), timeout=30)
    assert rec and rec["status"] == "ok", rec
    assert rec["kind"] == "import" and rec["streams"]["kg_nodes"] == 3
    assert _eventually(lambda: int(client.query(
        f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0]) == 3)
    # the unpacked bundle is gone after the job
    assert _eventually(lambda: list((tmp_path / "scratch").iterdir()) == [])


def test_import_rejects_bad_archive_before_writing(tp, monkeypatch, admin_hdr, tmp_path):
    import io
    import json
    import tarfile
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    c, _ = _client(tp, monkeypatch)

    r = c.post("/api/corpus/import", headers=admin_hdr,
               files={"file": ("x.tar.gz", b"not an archive", "application/gzip")})
    assert r.status_code == 400 and "invalid bundle" in r.text

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        m = json.dumps({"streams": {"kg_nodes": {"file": "../kg_nodes.parquet", "columns": [], "rows": 0}}}).encode()
        for name, data in [("manifest.json", m), ("../kg_nodes.parquet", b"x")]:
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    r = c.post("/api/corpus/import", headers=admin_hdr,
               files={"file": ("x.tar.gz", buf.getvalue(), "application/gzip")})
    assert r.status_code == 400 and "unsafe name" in r.text
    assert c.get("/api/jobs", headers=admin_hdr).json() == []
    assert list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "kg_nodes.parquet").exists()


def test_import_refused_while_a_job_is_active(tp, monkeypatch, admin_hdr, tmp_path):
    import threading
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    _seed_graph(client, prefix)
    c, _ = _client(tp, monkeypatch)
    archive = c.get("/api/corpus/export", headers=admin_hdr).content

    import tpk.api as api_mod
    from tpk.ingest import IngestResult
    gate = threading.Event()

    def slow_ingest(client_, cfg, prefix="", **kw):
        gate.wait(10)
        from tpk.config import entry_key
        return IngestResult(entry_key(cfg), "j", 1, 0, "ok")
    monkeypatch.setattr(api_mod, "ingest_repo", slow_ingest)

    job = c.post("/api/repos/reindex", headers=admin_hdr, json={"name": "demo", "ref": "v1"}).json()["job_id"]
    _eventually(lambda: c.get(f"/api/jobs/{job}", headers=admin_hdr).json()["status"] == "running")
    r = c.post("/api/corpus/import", headers=admin_hdr,
               files={"file": ("corpus.tar.gz", archive, "application/gzip")})
    assert r.status_code == 409
    gate.set()
    _eventually(lambda: c.get(f"/api/jobs/{job}", headers=admin_hdr).json()["status"] == "ok")
    assert c.post("/api/corpus/import", headers=admin_hdr,
                  files={"file": ("corpus.tar.gz", archive, "application/gzip")}).status_code == 200


def test_import_upload_size_cap(tp, monkeypatch, admin_hdr, tmp_path):
    _transfer_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TPK_IMPORT_MAX_MB", "1")
    c, _ = _client(tp, monkeypatch)
    r = c.post("/api/corpus/import", headers=admin_hdr,
               files={"file": ("big.tar.gz", b"\\0" * (2 * 1024 * 1024), "application/gzip")})
    assert r.status_code == 413 and "1 MB" in r.text
    assert list((tmp_path / "scratch").iterdir()) == []


def test_export_import_are_admin_only(tp, monkeypatch, admin_hdr):
    client, prefix = tp
    c, _ = _client(tp, monkeypatch)
    # a full corpus:manage role is still not enough -- the bundle is the whole graph
    auth.upsert_role(client, auth.Role("curator", [], capabilities=[auth.CAP_CORPUS_MANAGE]), prefix=prefix)
    auth.upsert_user(client, auth.User("cur", auth.hash_password("password-1"), "curator"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "cur", "password": "password-1"}).status_code == 200)
    t = c.post("/auth/login", json={"username": "cur", "password": "password-1"}).json()["token"]
    hdr = {"Authorization": f"Bearer {t}"}
    assert c.get("/api/corpus/export").status_code == 401
    assert c.get("/api/corpus/export", headers=hdr).status_code == 403
    assert c.post("/api/corpus/import", headers=hdr,
                  files={"file": ("x.tar.gz", b"x", "application/gzip")}).status_code == 403


# -- entry-filtered export / import (#104) ------------------------------------

def _seed_second_entry(client, prefix):
    from datetime import datetime, timezone
    from tpk import corpus
    from tpk.config import RepoConfig
    from tpk.model import NODE_COLUMNS, Node, node_rows
    now = datetime.now(timezone.utc)
    nodes = [Node(f"m{i}", "other@v2", "function", f"g{i}", f"other::g{i}", "src/y.cpp",
                  i, i + 2, f"other {i}", "c1", "public") for i in range(2)]
    client.insert(f"{prefix}kg_nodes", node_rows(nodes, now), column_names=NODE_COLUMNS)
    corpus.upsert_entry(client, RepoConfig(name="other", ref="v2", github="org/other"), prefix=prefix)
    _eventually(lambda: int(client.query(
        f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0]) == 5)


def _manifest_of(archive: bytes) -> dict:
    import io
    import json
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return json.load(tar.extractfile("manifest.json"))


def _node_repos(client, prefix):
    from tpk import db
    rows = client.query(f"SELECT DISTINCT repo FROM {db.latest(prefix + 'kg_nodes')}").result_rows
    return sorted(r[0] for r in rows)


def test_export_selected_entries(tp, monkeypatch, admin_hdr, tmp_path):
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    _seed_graph(client, prefix)
    _seed_second_entry(client, prefix)
    c, _ = _client(tp, monkeypatch)

    r = c.get("/api/corpus/export", headers=admin_hdr, params={"entries": ["other@v2"]})
    assert r.status_code == 200, r.text
    assert "-partial.tar.gz" in r.headers["content-disposition"]
    m = _manifest_of(r.content)
    assert m["partial"] is True and m["entries"] == ["other@v2"]
    assert m["streams"]["kg_nodes"]["rows"] == 2
    assert m["streams"]["kg_repos"]["rows"] == 1

    full = _manifest_of(c.get("/api/corpus/export", headers=admin_hdr).content)
    assert full["partial"] is False and full["entries"] == ["demo@v1", "other@v2"]

    r = c.get("/api/corpus/export", headers=admin_hdr, params={"entries": ["nope@v0", "other@v2"]})
    assert r.status_code == 422 and "nope@v0" in r.text


def test_import_selected_entries(tp, monkeypatch, admin_hdr, tmp_path):
    client, prefix = tp
    _transfer_env(monkeypatch, tmp_path)
    _seed_graph(client, prefix)
    _seed_second_entry(client, prefix)
    c, _ = _client(tp, monkeypatch)
    archive = c.get("/api/corpus/export", headers=admin_hdr).content

    # a key the bundle does not hold is refused before any job starts
    r = c.post("/api/corpus/import", headers=admin_hdr, data={"entries": ["ghost@v9"]},
               files={"file": ("corpus.tar.gz", archive, "application/gzip")})
    assert r.status_code == 422 and "ghost@v9" in r.text
    assert list((tmp_path / "scratch").iterdir()) == []

    # drop everything, load only other@v2 back
    from tpk import db
    for s in ("kg_nodes", "kg_edges", "kg_repos"):
        client.command(f"DROP STREAM IF EXISTS {prefix}{s}")
    db.ensure_schema(client, prefix)
    _eventually(lambda: _node_repos(client, prefix) == [])

    r = c.post("/api/corpus/import", headers=admin_hdr, data={"entries": ["other@v2"]},
               files={"file": ("corpus.tar.gz", archive, "application/gzip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries"] == ["other@v2"] and body["bundle_entries"] == ["demo@v1", "other@v2"]
    job_id = body["job_id"]
    rec = _eventually(lambda: (lambda j: j if j["status"] in ("ok", "failed") else None)(
        c.get(f"/api/jobs/{job_id}", headers=admin_hdr).json()), timeout=90)
    assert rec and rec["status"] == "ok", rec
    assert rec["entries"] == ["other@v2"]
    assert _eventually(lambda: _node_repos(client, prefix) == ["other@v2"])
    repos = c.get("/api/repos", headers=admin_hdr).json()
    assert [e["name"] for e in repos] == ["other"]
    assert _eventually(lambda: list((tmp_path / "scratch").iterdir()) == [])
