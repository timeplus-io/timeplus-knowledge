"""Pluggable DB backend (#50): unit tests for the per-backend SQL the db.py
abstraction generates, plus backend-parametrized integration tests that run the
real auth/corpus flows against a timeplusd and/or proton instance."""

import time

import pytest

from tpk import db


# -- unit: per-backend SQL generation (no DB) --------------------------------


def test_keyed_stream_ddl_timeplusd(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "timeplusd")
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    ddl = db._keyed_stream("p_", "kg_users", ["username string", "role string"], pk="username")
    # streams live under the `tpk` database, qualified as <db>.<prefix><name> (#58)
    assert "CREATE MUTABLE STREAM IF NOT EXISTS tpk.p_kg_users" in ddl
    assert "PRIMARY KEY (username)" in ddl
    assert "versioned_kv" not in ddl
    assert "deleted" not in ddl


def test_keyed_stream_ddl_proton(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "proton")
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    ddl = db._keyed_stream("p_", "kg_users", ["username string", "role string"], pk="username")
    assert "CREATE STREAM IF NOT EXISTS tpk.p_kg_users" in ddl
    assert "MUTABLE" not in ddl
    assert "deleted uint8 DEFAULT 0" in ddl
    assert "SETTINGS mode='versioned_kv'" in ddl


def test_qualified_name(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    assert db.qualified("kg_nodes") == "tpk.kg_nodes"
    assert db.qualified("kg_nodes", "p_") == "tpk.p_kg_nodes"
    monkeypatch.setenv("TIMEPLUS_DATABASE", "kb")
    assert db.qualified("kg_nodes", "p_") == "kb.p_kg_nodes"


def test_latest_timeplusd(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "timeplusd")
    assert db.latest("p_kg_users") == "table(p_kg_users)"


def test_latest_proton_filters_deleted(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "proton")
    assert db.latest("p_kg_users") == "(SELECT * FROM table(p_kg_users) WHERE deleted = 0)"


class _FakeClient:
    def __init__(self, rows=None):
        self.commands = []
        self.inserts = []
        self._rows = rows or []

    def command(self, sql, parameters=None):
        self.commands.append((sql, parameters))

    def query(self, sql, parameters=None):
        self.queries = getattr(self, "queries", [])
        self.queries.append((sql, parameters))
        return type("R", (), {"result_rows": self._rows})()

    def insert(self, stream, rows, column_names):
        self.inserts.append((stream, rows, column_names))


def test_delete_timeplusd_uses_real_delete(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "timeplusd")
    c = _FakeClient()
    db.delete(c, "p_kg_users", "username = %(u)s", {"u": "bob"}, ("username",))
    assert c.commands == [("DELETE FROM p_kg_users WHERE username = %(u)s", {"u": "bob"})]
    assert c.inserts == []


def test_delete_proton_tombstones_matching_keys(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "proton")
    # Two live rows match the WHERE -> tombstone both by PK with deleted=1.
    c = _FakeClient(rows=[("bob",), ("carol",)])
    db.delete(c, "p_kg_users", "role = %(r)s", {"r": "customer"}, ("username",))
    assert c.commands == []  # no real DELETE
    stream, rows, cols = c.inserts[0]
    assert stream == "p_kg_users"
    assert cols == ["username", "deleted"]
    assert rows == [["bob", 1], ["carol", 1]]


def test_delete_proton_noop_when_nothing_matches(monkeypatch):
    monkeypatch.setenv("TPK_DB_BACKEND", "proton")
    c = _FakeClient(rows=[])
    db.delete(c, "p_kg_users", "username = %(u)s", {"u": "ghost"}, ("username",))
    assert c.inserts == []  # nothing to tombstone


# -- integration: real auth/corpus flow on each configured backend -----------


def _eventually(fn, tries=10, delay=0.5):
    """Poll `fn` until it returns truthy (proton's versioned_kv table() reads
    are eventually consistent). Returns the last value."""
    val = None
    for _ in range(tries):
        val = fn()
        if val:
            return val
        time.sleep(delay)
    return val


def test_backend_upsert_update_delete_flow(backend_tp):
    from tpk import auth
    client, prefix, backend = backend_tp

    # upsert two users
    auth.upsert_user(client, auth.User("alice", "h1", "admin"), prefix=prefix)
    auth.upsert_user(client, auth.User("bob", "h2", "customer"), prefix=prefix)
    got = _eventually(lambda: auth.get_user(client, "alice", prefix=prefix))
    assert got is not None and got.role == "admin"

    # update alice (upsert same PK) -> latest wins
    auth.upsert_user(client, auth.User("alice", "h1b", "customer"), prefix=prefix)
    got = _eventually(lambda: (
        u if (u := auth.get_user(client, "alice", prefix=prefix)) and u.role == "customer" else None
    ))
    assert got is not None and got.role == "customer"

    # delete bob -> hidden from get + list (tombstone on proton, DELETE on timeplusd)
    auth.delete_user(client, "bob", prefix=prefix)
    _eventually(lambda: auth.get_user(client, "bob", prefix=prefix) is None)
    assert auth.get_user(client, "bob", prefix=prefix) is None
    assert sorted(u.username for u in auth.list_users(client, prefix=prefix)) == ["alice"]


def test_backend_corpus_delete_flow(backend_tp):
    from tpk import corpus
    from tpk.config import RepoConfig
    client, prefix, backend = backend_tp

    corpus.upsert_entry(client, RepoConfig(name="r1", ref="v1", enabled=True), prefix=prefix)
    corpus.upsert_entry(client, RepoConfig(name="r2", ref="v1", enabled=True), prefix=prefix)
    names = _eventually(lambda: (
        n if len(n := sorted(e.name for e in corpus.list_entries(client, prefix=prefix))) == 2 else None
    ))
    assert names == ["r1", "r2"]

    corpus.delete_entry(client, "r2", "v1", prefix=prefix)
    _eventually(lambda: (
        True if sorted(e.name for e in corpus.list_entries(client, prefix=prefix)) == ["r1"] else None
    ))
    assert sorted(e.name for e in corpus.list_entries(client, prefix=prefix)) == ["r1"]


def test_backend_session_lifecycle(backend_tp):
    from tpk import auth
    client, prefix, backend = backend_tp

    token = auth.create_session(client, "alice", 3600, prefix=prefix)
    assert _eventually(lambda: auth.get_session(client, token, prefix=prefix)) == "alice"

    auth.delete_session(client, token, prefix=prefix)
    _eventually(lambda: True if auth.get_session(client, token, prefix=prefix) is None else None)
    assert auth.get_session(client, token, prefix=prefix) is None


def test_backend_role_daily_token_limit_roundtrip(backend_tp):
    from tpk import auth
    client, prefix, backend = backend_tp

    auth.upsert_role(client, auth.Role("member", ["r1@v1"], "desc", ["chat"],
                                       daily_token_limit=12345), prefix=prefix)
    got = _eventually(lambda: auth.get_role(client, "member", prefix=prefix))
    assert got is not None and got.daily_token_limit == 12345
    # a role that doesn't set a limit reads back as 0 (unlimited)
    auth.upsert_role(client, auth.Role("open", [], "", ["chat"]), prefix=prefix)
    got2 = _eventually(lambda: auth.get_role(client, "open", prefix=prefix))
    assert got2 is not None and got2.daily_token_limit == 0


def test_backend_chat_usage_daily_count(backend_tp):
    from tpk import usage
    client, prefix, backend = backend_tp

    usage.record_usage(client, "bob", 1000, prefix=prefix)
    usage.record_usage(client, "bob", 500, prefix=prefix)
    usage.record_usage(client, "alice", 999, prefix=prefix)
    got = None
    for _ in range(20):
        got = usage.tokens_used_today(client, "bob", prefix=prefix)
        if got == 1500:
            break
        time.sleep(0.3)
    assert got == 1500
    # scoping is per-user
    assert usage.tokens_used_today(client, "alice", prefix=prefix) == 999
