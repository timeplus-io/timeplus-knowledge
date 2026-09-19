import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from test_graph_api import SEED_EDGES, SEED_NODES, _eventually, _NoAgent, _seed_corpus_entry
from tpk import auth
from tpk.ingest import upsert_graph
from tpk.server import create_app
from tpk.tools import KnowledgeGraph

pytestmark = requires_timeplus

H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(c, token, method, params=None, id_=1):
    headers = dict(H)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", headers=headers,
                  json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def _call(c, token, tool, args):
    r = _rpc(c, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result.get("isError", False), json.dumps(result)


def _seed(tp, tmp_path):
    client, prefix = tp
    upsert_graph(client, prefix, SEED_NODES, SEED_EDGES, datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "alpha", "v1", enabled=True)
    beta = tmp_path / "beta_checkout"
    beta.mkdir()
    (beta / "b.py").write_text("b1\nb2\nb3\nb4\nb5\n")
    _seed_corpus_entry(client, prefix, "beta", "v1", enabled=True, path=beta)
    _eventually(lambda: client.query(f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0],
                lambda v: v == len(SEED_NODES))
    _eventually(lambda: client.query(f"SELECT count() FROM table({prefix}kg_edges)").result_rows[0][0],
                lambda v: v == len(SEED_EDGES))
    return KnowledgeGraph(client, stream_prefix=prefix, repo_paths={}, corpus_ttl=0)


@pytest.fixture()
def env(tp, tmp_path):
    client, prefix = tp
    kg = _seed(tp, tmp_path)
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1"], capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_role(client, auth.Role("beta-src", ["beta@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_SOURCE_VIEW]), prefix=prefix)
    auth.upsert_role(client, auth.Role("chatter", ["alpha@v1"], capabilities=[auth.CAP_CHAT]), prefix=prefix)
    users = {"root": auth.ROLE_ADMIN, "scoped": "alpha-only", "betasrc": "beta-src", "carl": "chatter"}
    tokens = {}
    for name, role in users.items():
        auth.upsert_user(client, auth.User(name, auth.hash_password("password-1"), role), prefix=prefix)
        tokens[name], _ = auth.create_api_token(client, name, "t", prefix=prefix)
    for name, tok in tokens.items():
        _eventually(lambda t=tok: auth.resolve_api_token(client, t, prefix=prefix))
        _eventually(lambda n=name: auth.get_user(client, n, prefix=prefix))
    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=kg)
    with TestClient(app) as c:
        yield c, client, prefix, tokens


def test_auth_matrix(env):
    c, client, prefix, tokens = env
    r = _rpc(c, None, "tools/list")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert _rpc(c, "tpk_bogus", "tools/list").status_code == 401
    # a login SESSION token is not an API token
    session = c.post("/auth/login", json={"username": "root", "password": "password-1"}).json()["token"]
    assert _rpc(c, session, "tools/list").status_code == 401
    # no `explore`
    r = _rpc(c, tokens["carl"], "tools/list")
    assert r.status_code == 403 and "explore" in r.text
    # must_change_password
    auth.upsert_user(client, auth.User("scoped", auth.hash_password("password-1"), "alpha-only",
                                       must_change_password=True), prefix=prefix)
    r = _eventually(lambda: _rpc(c, tokens["scoped"], "tools/list"), lambda r: r.status_code == 403)
    assert r.json()["detail"]["code"] == "password_change_required"
    # disabled
    auth.upsert_user(client, auth.User("betasrc", auth.hash_password("password-1"), "beta-src",
                                       disabled=True), prefix=prefix)
    assert _eventually(lambda: _rpc(c, tokens["betasrc"], "tools/list"),
                       lambda r: r.status_code == 401).status_code == 401


def test_initialize_and_lists_six_tools(env):
    c, _, _, tokens = env
    init = _rpc(c, tokens["root"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"}})
    assert init.status_code == 200 and "result" in init.json()
    names = {t["name"] for t in _rpc(c, tokens["root"], "tools/list").json()["result"]["tools"]}
    assert names == {"search_entities", "get_entity", "neighbors",
                     "path_between", "list_communities", "read_source"}


def test_admin_unscoped_and_scoped_user_isolated_per_tool(env):
    c, _, _, tokens = env
    root, scoped = tokens["root"], tokens["scoped"]

    _, out = _call(c, root, "search_entities", {"query": "Widget"})
    assert "AlphaWidget" in out and "BetaWidget" in out
    _, out = _call(c, scoped, "search_entities", {"query": "Widget"})
    assert "AlphaWidget" in out and "BetaWidget" not in out

    _, out = _call(c, root, "get_entity", {"entity_id": "gbn1"})
    assert "BetaWidget" in out
    _, out = _call(c, scoped, "get_entity", {"entity_id": "gbn1"})
    assert "BetaWidget" not in out

    _, out = _call(c, scoped, "neighbors", {"entity_id": "gan1"})
    assert "AlphaHelper" in out
    _, out = _call(c, scoped, "neighbors", {"entity_id": "gbn1"})
    assert "BetaWidget" not in out

    _, out = _call(c, scoped, "path_between", {"id_a": "gan1", "id_b": "gan2"})
    assert "gan2" in out
    _, out = _call(c, scoped, "path_between", {"id_a": "gan1", "id_b": "gbn1"})
    assert "gbn1" not in out or "null" in out

    _, out = _call(c, root, "list_communities", {})
    assert "beta@v1" in out
    _, out = _call(c, scoped, "list_communities", {})
    assert "alpha@v1" in out and "beta@v1" not in out


def test_read_source_needs_source_view_and_scope(env):
    c, _, _, tokens = env
    args = {"repo": "beta@v1", "file_path": "b.py", "line_start": 1, "line_end": 2}
    is_err, out = _call(c, tokens["root"], "read_source", args)
    assert not is_err and "b1" in out
    is_err, out = _call(c, tokens["betasrc"], "read_source", args)
    assert not is_err and "b1" in out
    # `scoped` lacks source:view -> tool error, no source
    is_err, out = _call(c, tokens["scoped"], "read_source", args)
    assert is_err and "source:view" in out and "b1" not in out


def test_role_change_applies_on_next_call(env):
    c, client, prefix, tokens = env
    _, out = _call(c, tokens["scoped"], "search_entities", {"query": "Widget"})
    assert "BetaWidget" not in out
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1", "beta@v1"],
                                       capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    out = _eventually(lambda: _call(c, tokens["scoped"], "search_entities", {"query": "Widget"})[1],
                      lambda o: "BetaWidget" in o)
    assert "BetaWidget" in out


def test_revoked_token_401(env):
    c, client, prefix, tokens = env
    tid = auth.list_api_tokens(client, "root", prefix=prefix)[0].token_id
    auth.revoke_api_token(client, "root", tid, prefix=prefix)
    assert _eventually(lambda: _rpc(c, tokens["root"], "tools/list"),
                       lambda r: r.status_code == 401).status_code == 401


def test_store_down_503(tp, tmp_path):
    client, prefix = tp
    kg = _seed(tp, tmp_path)

    class DownAuth(auth.AuthLayer):
        def _client(self):
            raise RuntimeError("store down")

    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=kg, auth=DownAuth(prefix))
    with TestClient(app) as c:
        assert _rpc(c, "tpk_anything", "tools/list").status_code == 503


def test_disabled_by_config(tp, tmp_path, monkeypatch):
    monkeypatch.setenv("TPK_MCP_HTTP_ENABLED", "0")
    client, prefix = tp
    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=_seed(tp, tmp_path))
    with TestClient(app) as c:
        assert _rpc(c, "tpk_anything", "tools/list").status_code in (404, 405)
