from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import requires_timeplus
from tpk.model import Edge, Node

pytestmark = requires_timeplus

# Seed graph: a() -> b() -> c(), doc D documents a; a,b in community "core", c,D in "docs"
SEED_NODES = [
    Node(id="a1", repo="r1", kind="function", name="alpha", qualified_name="m.alpha",
         file_path="m.py", line_start=1, line_end=3, summary="checkpoint flush entry",
         community="core", visibility="internal"),
    Node(id="b1", repo="r1", kind="function", name="beta", qualified_name="m.beta",
         file_path="m.py", line_start=5, line_end=8, summary="writes checkpoint state",
         community="core", visibility="internal"),
    Node(id="c1", repo="r1", kind="function", name="gamma", qualified_name="m.gamma",
         file_path="m.py", line_start=10, line_end=12, summary="fsync helper",
         community="docs", visibility="internal"),
    Node(id="d1", repo="docs", kind="doc_section", name="Checkpointing",
         qualified_name="ops/ckpt.md#Checkpointing", file_path="ops/ckpt.md",
         line_start=1, line_end=30, summary="how checkpoints work",
         community="docs", visibility="public"),
]
SEED_EDGES = [
    Edge(src="a1", dst="b1", rel="calls", confidence="EXTRACTED", repo="r1"),
    Edge(src="b1", dst="c1", rel="calls", confidence="INFERRED", repo="r1"),
    Edge(src="d1", dst="a1", rel="documents", confidence="EXTRACTED", repo="docs"),
]


@pytest.fixture()
def kg(tp, tmp_path: Path):
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    upsert_graph(client, prefix, SEED_NODES, SEED_EDGES, datetime.now(timezone.utc))
    (tmp_path / "m.py").write_text("line1\nline2\nline3\nline4\nline5\n")

    def _node_count():
        return client.query(
            f"SELECT count() FROM table({prefix}kg_nodes)"
        ).result_rows[0][0]

    def _edge_count():
        return client.query(
            f"SELECT count() FROM table({prefix}kg_edges)"
        ).result_rows[0][0]

    # upsert_graph does two independent inserts (nodes, edges), each with its
    # own ~100-300ms mutable-stream visibility lag -- wait for both, not just
    # nodes, or edge-dependent tests (neighbors, path_between) can race.
    _eventually(_node_count, lambda v: v == len(SEED_NODES))
    _eventually(_edge_count, lambda v: v == len(SEED_EDGES))
    return KnowledgeGraph(client, stream_prefix=prefix, repo_paths={"r1": tmp_path})


def _eventually(fn, predicate, timeout=5.0, interval=0.1):
    """Poll fn() until predicate(result) holds, or return the last value on timeout.

    Mutable-stream writes have a small (~100-300ms) propagation lag before
    being visible to a fresh `table(...)` read; see tests/test_ingest.py.
    """
    import time

    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def test_search_all_tokens_must_match(kg):
    hits = kg.search_entities("checkpoint flush")
    assert [h["id"] for h in hits] == ["a1"]  # only alpha matches both tokens


def test_search_filters_by_kind_and_repo(kg):
    assert {h["id"] for h in kg.search_entities("checkpoint", kinds=["doc_section"])} == {"d1"}
    assert {h["id"] for h in kg.search_entities("checkpoint", repos=["docs"])} == {"d1"}


def test_get_entity(kg):
    assert kg.get_entity("a1")["name"] == "alpha"
    assert kg.get_entity("nope") is None


def test_neighbors_depth_and_confidence(kg):
    one_hop = kg.neighbors("a1", depth=1)
    assert {n["id"] for n in one_hop["nodes"]} == {"a1", "b1", "d1"}
    two_hop = kg.neighbors("a1", depth=2)
    assert "c1" in {n["id"] for n in two_hop["nodes"]}
    extracted_only = kg.neighbors("a1", depth=2, confidence="EXTRACTED")
    assert "c1" not in {n["id"] for n in extracted_only["nodes"]}  # b->c is INFERRED


def test_neighbors_depth_is_capped(kg):
    from tpk.tools import KnowledgeGraph

    result = kg.neighbors("a1", depth=99)
    assert result["depth_used"] == KnowledgeGraph.MAX_DEPTH


def test_path_between(kg):
    path = kg.path_between("d1", "c1")
    ids = [p["id"] for p in path if "id" in p]
    assert ids[0] == "d1" and ids[-1] == "c1"
    assert kg.path_between("a1", "missing") is None


def test_path_between_max_depth_is_clamped(kg):
    # d1 -> a1 -> b1 -> c1 is 3 hops; max_depth=99 must be clamped to
    # MAX_PATH_DEPTH (6) rather than accepted verbatim, and the batched BFS
    # must still find the path within the clamp.
    path = kg.path_between("d1", "c1", max_depth=99)
    ids = [p["id"] for p in path if "id" in p]
    assert ids[0] == "d1" and ids[-1] == "c1"


def test_path_between_clamp_can_make_target_unreachable(kg, monkeypatch):
    from tpk.tools import KnowledgeGraph

    # d1 -> c1 needs 3 hops; clamping MAX_PATH_DEPTH to 1 must make it
    # unreachable even though the caller asked for max_depth=99.
    monkeypatch.setattr(KnowledgeGraph, "MAX_PATH_DEPTH", 1)
    assert kg.path_between("d1", "c1", max_depth=99) is None


def test_dangling_edge_dropped_from_neighbors_and_breaks_path(kg):
    """An edge whose endpoint node row is missing (e.g. partial ingest
    failure) must not leak into neighbors()'s edge list, and a path routed
    through it must return None instead of raising KeyError.
    """
    from tpk.ingest import upsert_graph
    from tpk.model import Edge

    dangling = Edge(src="c1", dst="ghost1", rel="calls", confidence="EXTRACTED", repo="r1")
    # repos=set() (not None) skips upsert_graph's derive-and-delete-stale
    # step entirely, so the pre-existing seed edges are left untouched.
    upsert_graph(kg.client, kg.prefix, [], [dangling], datetime.now(timezone.utc), repos=set())

    def _dangling_edge_count():
        return kg.client.query(
            f"SELECT count() FROM table({kg.prefix}kg_edges) WHERE dst = 'ghost1'"
        ).result_rows[0][0]

    _eventually(_dangling_edge_count, lambda v: v == 1)

    result = kg.neighbors("c1", depth=1)
    node_ids = {n["id"] for n in result["nodes"]}
    assert "ghost1" not in node_ids
    for e in result["edges"]:
        assert e["src"] != "ghost1" and e["dst"] != "ghost1"

    assert kg.path_between("b1", "ghost1", max_depth=3) is None


def test_list_communities(kg):
    rows = kg.list_communities(repo="r1")
    by_name = {r["community"]: r["node_count"] for r in rows}
    assert by_name == {"core": 2, "docs": 1}


def test_neighbors_edges_stay_consistent_with_truncated_nodes(kg, monkeypatch):
    """When a hop's frontier is truncated to MAX_NODES_PER_HOP, edges whose
    endpoint got dropped from the frontier must be dropped too -- otherwise
    `edges` can reference node ids absent from `nodes`.
    """
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    hub_nodes = [
        Node(id=f"n{i}", repo="r1", kind="function", name=f"leaf{i}",
             qualified_name=f"m.leaf{i}", file_path="m.py", line_start=1, line_end=1,
             summary="leaf", community="core", visibility="internal")
        for i in range(1, 5)
    ] + [
        Node(id="h1", repo="r1", kind="function", name="hub", qualified_name="m.hub",
             file_path="m.py", line_start=1, line_end=1, summary="hub node",
             community="core", visibility="internal"),
    ]
    hub_edges = [
        Edge(src="h1", dst=f"n{i}", rel="calls", confidence="EXTRACTED", repo="r1")
        for i in range(1, 5)
    ]
    upsert_graph(kg.client, kg.prefix, hub_nodes, hub_edges, datetime.now(timezone.utc))

    def _hub_node_count():
        return kg.client.query(
            f"SELECT count() FROM table({kg.prefix}kg_nodes) WHERE id LIKE 'n%' OR id = 'h1'"
        ).result_rows[0][0]

    def _hub_edge_count():
        return kg.client.query(
            f"SELECT count() FROM table({kg.prefix}kg_edges) WHERE src = 'h1'"
        ).result_rows[0][0]

    _eventually(_hub_node_count, lambda v: v == 5)
    _eventually(_hub_edge_count, lambda v: v == 4)

    monkeypatch.setattr(KnowledgeGraph, "MAX_NODES_PER_HOP", 2)
    result = kg.neighbors("h1", depth=1)
    node_ids = {n["id"] for n in result["nodes"]}
    assert len(node_ids) <= 1 + KnowledgeGraph.MAX_NODES_PER_HOP
    for e in result["edges"]:
        assert e["src"] in node_ids
        assert e["dst"] in node_ids


def test_read_source(kg):
    text = kg.read_source("r1", "m.py", 2, 3)
    assert text == "line2\nline3\n"
    with pytest.raises(ValueError):
        kg.read_source("unknown", "m.py", 1, 2)
    with pytest.raises(ValueError):
        kg.read_source("r1", "../etc/passwd", 1, 2)


def test_read_source_output_is_capped(kg):
    from tpk.tools import KnowledgeGraph

    root = kg.repo_paths["r1"]
    (root / "big.py").write_text("".join(f"line{i}\n" for i in range(1, 501)))
    text = kg.read_source("r1", "big.py", 1, 500)
    lines = text.splitlines()
    assert len(lines) == KnowledgeGraph.MAX_SOURCE_LINES == 400
    assert lines[0] == "line1" and lines[-1] == "line400"
