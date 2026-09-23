"""GET /api/ingest-log (#15): persistent ingest history folded per run."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth, db
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


def _log(client, prefix, repo, run_id, status, nodes=0, edges=0, sha="abc1234", error="", at=None):
    cols = ["repo", "run_id", "nodes", "edges", "git_sha", "status", "error"]
    row = [repo, run_id, nodes, edges, sha, status, error]
    if at is not None:  # backdate: the stream's _tp_time is writable on insert
        cols.append("_tp_time"); row.append(at)
    client.insert(db.qualified("kg_ingest_log", prefix), [row], column_names=cols)


@pytest.fixture()
def env(tp):
    client, prefix = tp
    c = TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))
    auth.upsert_role(client, auth.Role("viewer", [], capabilities=[auth.CAP_CORPUS_VIEW]), prefix=prefix)
    auth.upsert_role(client, auth.Role("chatter", [], capabilities=[auth.CAP_CHAT]), prefix=prefix)
    for name, role in (("vi", "viewer"), ("ch", "chatter")):
        auth.upsert_user(client, auth.User(name, auth.hash_password("password-1"), role), prefix=prefix)

    def hdr(name):
        login = lambda: c.post("/auth/login", json={"username": name, "password": "password-1"})
        _eventually(lambda: login().status_code == 200)
        return {"Authorization": f"Bearer {login().json()['token']}"}

    return c, client, prefix, hdr


def _runs(c, h, **params):
    r = c.get("/api/ingest-log", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def test_requires_corpus_view(env):
    c, _, _, hdr = env
    assert c.get("/api/ingest-log").status_code == 401
    assert c.get("/api/ingest-log", headers=hdr("ch")).status_code == 403
    assert c.get("/api/ingest-log", headers=hdr("vi")).status_code == 200


def test_folds_start_and_end_rows_into_one_run_newest_first(env):
    c, client, prefix, hdr = env
    now = datetime.now(timezone.utc)
    _log(client, prefix, "alpha@v1", "r1", "started", at=now - timedelta(minutes=10))
    _log(client, prefix, "alpha@v1", "r1", "ok", nodes=120, edges=300, at=now - timedelta(minutes=6))
    _log(client, prefix, "beta@v1", "r2", "started", at=now - timedelta(minutes=5))
    _log(client, prefix, "beta@v1", "r2", "failed", error="RuntimeError: graphify exploded",
         at=now - timedelta(minutes=4))
    _log(client, prefix, "old@v0", "r0", "ok", nodes=5, edges=1, at=now - timedelta(days=3))  # pre-#15 row: no start
    runs = _eventually(lambda: _runs(c, hdr("vi")), lambda v: len(v) == 3)
    assert [r["run_id"] for r in runs] == ["r2", "r1", "r0"]
    r1 = runs[1]
    assert r1["entry_key"] == "alpha@v1" and r1["status"] == "ok"
    assert (r1["nodes"], r1["edges"], r1["git_sha"], r1["error"]) == (120, 300, "abc1234", "")
    assert r1["started_at"] < r1["finished_at"] and 235 <= r1["duration_s"] <= 245
    r2 = runs[0]
    assert r2["status"] == "failed" and "graphify exploded" in r2["error"]
    r0 = runs[2]
    assert r0["status"] == "ok" and r0["started_at"] is None and r0["duration_s"] is None


def test_running_and_stale(env):
    c, client, prefix, hdr = env
    now = datetime.now(timezone.utc)
    _log(client, prefix, "live@v1", "live", "started", at=now - timedelta(minutes=2))
    _log(client, prefix, "dead@v1", "dead", "started", at=now - timedelta(hours=5))
    runs = _eventually(lambda: _runs(c, hdr("vi")), lambda v: len(v) == 2)
    by = {r["run_id"]: r for r in runs}
    assert by["live"]["status"] == "running" and by["live"]["finished_at"] is None
    assert by["dead"]["status"] == "stale"
    assert "no end" in by["dead"]["error"].lower() or "did not finish" in by["dead"]["error"].lower()


def test_filter_and_limit(env):
    c, client, prefix, hdr = env
    now = datetime.now(timezone.utc)
    for i in range(5):
        _log(client, prefix, "a@v1" if i % 2 else "b@v1", f"r{i}", "ok", at=now - timedelta(minutes=10 - i))
    _eventually(lambda: _runs(c, hdr("vi")), lambda v: len(v) == 5)
    assert [r["run_id"] for r in _runs(c, hdr("vi"), limit=2)] == ["r4", "r3"]
    assert {r["entry_key"] for r in _runs(c, hdr("vi"), entry="a@v1")} == {"a@v1"}
    assert len(_runs(c, hdr("vi"), entry="a@v1")) == 2
    assert c.get("/api/ingest-log", headers=hdr("vi"), params={"limit": 0}).status_code == 422
    assert c.get("/api/ingest-log", headers=hdr("vi"), params={"limit": 10_000}).status_code == 422


def test_last_ingest_column_treats_an_abandoned_start_as_stale(env):
    from tpk import corpus
    from tpk.config import RepoConfig

    c, client, prefix, hdr = env
    corpus.upsert_entry(client, RepoConfig(name="dead", github="org/dead", ref="v1", visibility="internal"),
                        prefix=prefix)
    _log(client, prefix, "dead@v1", "d1", "ok", nodes=9, at=datetime.now(timezone.utc) - timedelta(days=1))
    _log(client, prefix, "dead@v1", "d2", "started", at=datetime.now(timezone.utc) - timedelta(hours=5))
    rows = _eventually(lambda: c.get("/api/repos", headers=hdr("vi")).json(),
                       lambda v: any(r["entry_key"] == "dead@v1" and r.get("last_status") for r in v))
    row = next(r for r in rows if r["entry_key"] == "dead@v1")
    assert row["last_status"] == "stale"
