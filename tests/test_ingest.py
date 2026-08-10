import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import requires_timeplus
from tpk.config import RepoConfig
from tpk.model import Edge, Node

pytestmark = requires_timeplus


def _node(nid: str, name: str, repo: str = "tinyrepo") -> Node:
    return Node(
        id=nid, repo=repo, kind="function", name=name,
        qualified_name=f"mod.{name}", file_path="mod.py",
        line_start=1, line_end=5, summary=f"summary of {name}",
        community="c1", visibility="internal",
    )


def _count(client, prefix, stream, repo):
    return client.query(
        f"SELECT count() FROM table({prefix}{stream}) WHERE repo = %(r)s",
        parameters={"r": repo},
    ).result_rows[0][0]


def _eventually(fn, predicate, timeout=5.0, interval=0.1):
    """Poll fn() until predicate(result) holds, or return the last value on timeout.

    This local Timeplus instance's mutable-stream writes/deletes have a small
    (observed ~100-300ms, occasionally more under shared-box load) propagation
    lag before being visible to a fresh `table(...)` read. This helper is a
    minimal, test-only adaptation to make assertions deterministic without
    changing the upsert/stale-delete behavior under test.
    """
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def test_upsert_then_stale_delete(tp):
    from tpk.ingest import upsert_graph

    client, prefix = tp
    t0 = datetime.now(timezone.utc)
    nodes = [_node("n1", "alpha"), _node("n2", "beta")]
    edges = [Edge(src="n1", dst="n2", rel="calls", confidence="EXTRACTED", repo="tinyrepo")]
    upsert_graph(client, prefix, nodes, edges, run_started_at=t0)
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 2
    ) == 2

    # second run: n2 disappeared, n1 updated -> n2 must be stale-deleted
    t1 = datetime.now(timezone.utc) + timedelta(seconds=1)
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], run_started_at=t1)
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 1
    ) == 1
    assert _eventually(
        lambda: _count(client, prefix, "kg_edges", "tinyrepo"), lambda v: v == 0
    ) == 0


def test_ingest_repo_failure_leaves_graph_intact_and_logs(tp, monkeypatch, tmp_path: Path):
    from tpk import ingest as ingest_mod
    from tpk.ingest import ingest_repo, upsert_graph

    client, prefix = tp
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], datetime.now(timezone.utc))

    def boom(repo_path, out_dir):
        raise RuntimeError("graphify exploded")

    monkeypatch.setattr(ingest_mod, "run_graphify", boom)
    cfg = RepoConfig(name="tinyrepo", path=tmp_path, visibility="internal")
    result = ingest_repo(client, cfg, prefix=prefix, out_root=tmp_path / "out")
    assert result.status == "failed"
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 1
    ) == 1  # prior graph intact

    def _logs():
        return client.query(
            f"SELECT status FROM table({prefix}kg_ingest_log) WHERE repo = 'tinyrepo'"
        ).result_rows

    logs = _eventually(_logs, lambda rows: ("failed",) in rows)
    assert ("failed",) in logs
