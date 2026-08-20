import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth
from tpk.ingest import upsert_graph
from tpk.model import Edge, Node
from tpk.server import create_app
from tpk.tools import KnowledgeGraph

pytestmark = requires_timeplus


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    """Poll fn() until predicate(result) holds, or return the last value on
    timeout. Mutable-stream writes have a small (~100-300ms) propagation lag
    before being visible to a fresh `table(...)` read; see tests/test_tools.py.
    """
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


# Seed graph: two enabled corpus entries ("alpha@v1", "beta@v1"), one edge
# inside alpha@v1 (AlphaWidget -> AlphaHelper).
SEED_NODES = [
    Node(id="gan1", repo="alpha@v1", kind="function", name="AlphaWidget",
         qualified_name="alpha.AlphaWidget", file_path="w.py", line_start=1, line_end=5,
         summary="widget in alpha", community="core", visibility="internal"),
    Node(id="gan2", repo="alpha@v1", kind="function", name="AlphaHelper",
         qualified_name="alpha.AlphaHelper", file_path="w.py", line_start=7, line_end=9,
         summary="helper for alpha widget", community="core", visibility="internal"),
    Node(id="gbn1", repo="beta@v1", kind="function", name="BetaWidget",
         qualified_name="beta.BetaWidget", file_path="b.py", line_start=1, line_end=3,
         summary="widget in beta", community="core", visibility="internal"),
]
SEED_EDGES = [
    Edge(src="gan1", dst="gan2", rel="calls", confidence="EXTRACTED", repo="alpha@v1"),
]


def _seed_corpus_entry(client, prefix, name, ref, enabled=True, path=None):
    from tpk import corpus
    from tpk.config import RepoConfig

    # `path=None` (the default) seeds a github-sourced entry, as before --
    # its resolved checkout path doesn't exist on disk (fine for the tests
    # that only care about search/entity/neighbors filtering). Passing
    # `path` seeds a local-path entry instead, whose `resolved_repo_path`
    # is exactly that directory -- needed for a `/source` test to be a real
    # scope check, not just an "unknown repo"/"file not found" 404 either
    # way.
    cfg = (
        RepoConfig(name=name, path=path, ref=ref, visibility="internal", enabled=enabled)
        if path is not None
        else RepoConfig(name=name, github=f"org/{name}", ref=ref, visibility="internal", enabled=enabled)
    )
    corpus.upsert_entry(client, cfg, prefix=prefix)


@pytest.fixture()
def kg_app(tp, tmp_path):
    client, prefix = tp
    upsert_graph(client, prefix, SEED_NODES, SEED_EDGES, datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "alpha", "v1", enabled=True)
    beta_checkout = tmp_path / "beta_checkout"
    beta_checkout.mkdir()
    (beta_checkout / "b.py").write_text("b1\nb2\nb3\nb4\nb5\n")
    # beta@v1 is a real, readable local-path entry (unlike alpha@v1's fake
    # github checkout) so a scoped-non-admin 404 against it is provably
    # caused by role scoping, not by the file simply not existing -- see
    # test_admin_source_reads_beta_repo / test_non_admin_source_out_of_scope_404.
    _seed_corpus_entry(client, prefix, "beta", "v1", enabled=True, path=beta_checkout)
    (tmp_path / "w.py").write_text("line1\nline2\nline3\nline4\nline5\n")

    def _node_count():
        return client.query(f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0]

    def _edge_count():
        return client.query(f"SELECT count() FROM table({prefix}kg_edges)").result_rows[0][0]

    _eventually(_node_count, lambda v: v == len(SEED_NODES))
    _eventually(_edge_count, lambda v: v == len(SEED_EDGES))

    # "r1" (not a registered corpus entry -- see test_tools.py's
    # test_role_scope_applies_without_corpus_store) so `read_source` falls
    # through to this static path instead of a corpus-derived (and, for a
    # fake github-sourced entry, nonexistent) checkout path.
    kg = KnowledgeGraph(client, stream_prefix=prefix, repo_paths={"r1": tmp_path}, corpus_ttl=0)
    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=kg)
    c = TestClient(app)
    return c, client, prefix


@pytest.fixture()
def admin_hdr(kg_app):
    c, client, prefix = kg_app
    auth.upsert_user(client, auth.User("root", auth.hash_password("adminpass1"), auth.ROLE_ADMIN), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def scoped_hdr(kg_app):
    """A non-admin user whose role lists only the alpha@v1 entry -- beta@v1
    (and anything else) is out of scope for it."""
    c, client, prefix = kg_app
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_SOURCE_VIEW]), prefix=prefix)
    auth.upsert_user(client, auth.User("scoped", auth.hash_password("password-1"), "alpha-only"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "scoped", "password": "password-1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "scoped", "password": "password-1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_401_on_all_routes(kg_app):
    c, _, _ = kg_app
    assert c.get("/api/graph/search", params={"q": "Alpha"}).status_code == 401
    assert c.get("/api/graph/entity", params={"id": "gan1"}).status_code == 401
    assert c.get("/api/graph/neighbors", params={"id": "gan1"}).status_code == 401
    assert c.get("/api/graph/source", params={
        "repo": "r1", "file_path": "w.py", "line_start": 1, "line_end": 2,
    }).status_code == 401


def test_explore_capability_required(kg_app):
    """A non-admin whose role lacks `explore` is refused on every graph route
    (capability gate fires before scoping)."""
    c, client, prefix = kg_app
    auth.upsert_role(client, auth.Role("no-explore", ["alpha@v1"],
                                       capabilities=[auth.CAP_CHAT]), prefix=prefix)
    auth.upsert_user(client, auth.User("noexp", auth.hash_password("password-1"),
                                       "no-explore"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login",
                json={"username": "noexp", "password": "password-1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "noexp", "password": "password-1"}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/graph/search", params={"q": "Alpha"}, headers=hdr).status_code == 403
    assert c.get("/api/graph/entity", params={"id": "gan1"}, headers=hdr).status_code == 403
    assert c.get("/api/graph/neighbors", params={"id": "gan1"}, headers=hdr).status_code == 403
    assert c.get("/api/graph/source", params={
        "repo": "alpha@v1", "file_path": "a.py", "line_start": 1, "line_end": 2,
    }, headers=hdr).status_code == 403


def test_admin_search_returns_seeded_entity(kg_app, admin_hdr):
    c, _, _ = kg_app
    r = c.get("/api/graph/search", params={"q": "AlphaWidget"}, headers=admin_hdr)
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    hit = results[0]
    assert hit["id"] == "gan1"
    assert hit["kind"] == "function"
    assert hit["name"] == "AlphaWidget"
    assert hit["repo"] == "alpha@v1"


def test_admin_entity_get_and_404(kg_app, admin_hdr):
    c, _, _ = kg_app
    r = c.get("/api/graph/entity", params={"id": "gan1"}, headers=admin_hdr)
    assert r.status_code == 200
    assert r.json()["name"] == "AlphaWidget"
    assert c.get("/api/graph/entity", params={"id": "bogus-id"}, headers=admin_hdr).status_code == 404


def test_admin_neighbors_returns_edges_with_direction(kg_app, admin_hdr):
    c, _, _ = kg_app
    r = c.get("/api/graph/neighbors", params={"id": "gan1"}, headers=admin_hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["center"]["id"] == "gan1"
    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert edge["rel"] == "calls"
    assert edge["confidence"] == "EXTRACTED"
    assert edge["direction"] == "out"
    assert edge["other"]["id"] == "gan2"
    assert edge["other"]["name"] == "AlphaHelper"


def test_admin_neighbors_bogus_id_404(kg_app, admin_hdr):
    c, _, _ = kg_app
    assert c.get("/api/graph/neighbors", params={"id": "bogus-id"}, headers=admin_hdr).status_code == 404


def test_admin_source_returns_lines(kg_app, admin_hdr):
    c, _, _ = kg_app
    r = c.get("/api/graph/source", params={
        "repo": "r1", "file_path": "w.py", "line_start": 2, "line_end": 3,
    }, headers=admin_hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["lines"] == "line2\nline3\n"
    assert body["repo"] == "r1"
    assert body["line_start"] == 2 and body["line_end"] == 3


def test_source_unknown_repo_404(kg_app, admin_hdr):
    c, _, _ = kg_app
    r = c.get("/api/graph/source", params={
        "repo": "unknown@v1", "file_path": "w.py", "line_start": 1, "line_end": 2,
    }, headers=admin_hdr)
    assert r.status_code == 404


def test_admin_source_reads_beta_repo(kg_app, admin_hdr):
    """Sanity check backing test_non_admin_source_out_of_scope_404: beta@v1
    is a real, readable local-path entry for an unscoped (admin) caller, so
    that test's 404 for a scoped caller is provably a scope block, not a
    missing/misconfigured file."""
    c, _, _ = kg_app
    r = c.get("/api/graph/source", params={
        "repo": "beta@v1", "file_path": "b.py", "line_start": 1, "line_end": 2,
    }, headers=admin_hdr)
    assert r.status_code == 200
    assert r.json()["lines"] == "b1\nb2\n"


def test_non_admin_scoped_to_role_entries(kg_app, scoped_hdr):
    c, _, _ = kg_app
    # "Widget" matches both AlphaWidget (alpha@v1) and BetaWidget (beta@v1);
    # the scoped role only lists alpha@v1, so beta must be excluded.
    r = c.get("/api/graph/search", params={"q": "Widget"}, headers=scoped_hdr)
    assert r.status_code == 200
    repos = {h["repo"] for h in r.json()["results"]}
    assert repos == {"alpha@v1"}

    assert c.get("/api/graph/entity", params={"id": "gbn1"}, headers=scoped_hdr).status_code == 404
    assert c.get("/api/graph/entity", params={"id": "gan1"}, headers=scoped_hdr).status_code == 200


def test_non_admin_neighbors_out_of_scope_404(kg_app, scoped_hdr):
    """gbn1 (beta@v1) is outside the scoped role's only entry (alpha@v1) --
    it's filtered out of kg.neighbors()'s returned nodes, so the center
    lookup must 404 exactly like /entity does for the same id, not leak
    edges/attributes for a node the caller can't otherwise see."""
    c, _, _ = kg_app
    assert c.get("/api/graph/neighbors", params={"id": "gbn1"}, headers=scoped_hdr).status_code == 404
    # The in-scope center still works for the same scoped caller.
    r = c.get("/api/graph/neighbors", params={"id": "gan1"}, headers=scoped_hdr)
    assert r.status_code == 200
    assert r.json()["center"]["id"] == "gan1"


def test_non_admin_source_out_of_scope_404(kg_app, scoped_hdr):
    """beta@v1 is real and admin-readable (test_admin_source_reads_beta_repo)
    -- a scoped caller whose role only lists alpha@v1 must still be refused."""
    c, _, _ = kg_app
    r = c.get("/api/graph/source", params={
        "repo": "beta@v1", "file_path": "b.py", "line_start": 1, "line_end": 3,
    }, headers=scoped_hdr)
    assert r.status_code == 404


def test_source_requires_source_view_cap(kg_app):
    """`/source` now requires source:view; a role with explore-only can still
    search/entity/neighbors but is refused the raw source body."""
    c, client, prefix = kg_app
    auth.upsert_role(client, auth.Role("explore-only", ["r1"],
                                       capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_user(client, auth.User("exp", auth.hash_password("password-1"),
                                       "explore-only"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login",
                json={"username": "exp", "password": "password-1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "exp", "password": "password-1"}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # search still works with explore alone...
    assert c.get("/api/graph/search", params={"q": "AlphaWidget"}, headers=hdr).status_code == 200
    # ...but the source body is gated behind source:view.
    assert c.get("/api/graph/source", params={
        "repo": "r1", "file_path": "w.py", "line_start": 1, "line_end": 2,
    }, headers=hdr).status_code == 403
