"""`tpk serve` and the stdio MCP server must honour TPK_STREAM_PREFIX like the
rest of the CLI (#84) -- otherwise they silently run on the REAL streams."""
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import requires_timeplus
from tpk import auth
from tpk import cli as cli_mod
from tpk.cli import app
from tpk.ingest import upsert_graph
from tpk.model import Node

pytestmark = requires_timeplus


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def test_serve_runs_entirely_on_the_prefixed_streams(tp, monkeypatch, tmp_path):
    client, prefix = tp
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)
    toml = tmp_path / "repos.toml"
    (tmp_path / "checkout").mkdir()
    toml.write_text(f'[repos.prefixrepo]\npath = "{tmp_path / "checkout"}"\nvisibility = "internal"\n')
    monkeypatch.setattr(cli_mod, "REPOS_TOML", toml)
    import tpk.server as server_mod
    monkeypatch.setattr(server_mod, "REPOS_TOML", toml)

    # Things that exist ONLY under the prefix: a user and a graph node.
    auth.upsert_user(client, auth.User("prefix-only", auth.hash_password("password-1"),
                                       auth.ROLE_ADMIN), prefix=prefix)
    upsert_graph(client, prefix, [
        Node(id="pfx1", repo="prefixrepo", kind="function", name="OnlyUnderPrefix",
             qualified_name="p.OnlyUnderPrefix", file_path="p.py", line_start=1, line_end=2,
             summary="exists only in the prefixed streams", community="0", visibility="internal"),
    ], [], datetime.now(timezone.utc))

    served = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda a, **kw: served.update(app=a))
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 0, result.output

    # corpus seeding went to the prefixed registry
    from tpk import corpus
    assert _eventually(lambda: [e.name for e in corpus.list_entries(client, prefix=prefix)]) == ["prefixrepo"]

    c = TestClient(served["app"])
    login = lambda: c.post("/auth/login", json={"username": "prefix-only", "password": "password-1"})
    assert _eventually(login, lambda r: r.status_code == 200).status_code == 200   # auth store
    hdr = {"Authorization": f"Bearer {login().json()['token']}"}
    hits = _eventually(lambda: c.get("/api/graph/search", params={"q": "OnlyUnderPrefix"},
                                     headers=hdr).json()["results"])
    assert [h["id"] for h in hits] == ["pfx1"]                                      # graph


def test_stdio_mcp_server_uses_the_prefix(tp, monkeypatch, tmp_path):
    from tpk import mcp_server

    client, prefix = tp
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)
    toml = tmp_path / "repos.toml"
    toml.write_text("[repos]\n")
    monkeypatch.setattr(mcp_server, "REPOS_TOML", toml)

    built = {}

    class _Server:
        def run(self):
            pass

    monkeypatch.setattr(mcp_server, "build_server", lambda kg, guard=None: built.update(kg=kg) or _Server())
    mcp_server.main()
    assert built["kg"].prefix == prefix
