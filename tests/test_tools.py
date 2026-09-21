from dataclasses import replace
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


def test_search_repos_accepts_bare_name_or_entry_key(tp):
    """The `repos` filter matches a node keyed `name@ref` when given either the
    full entry key OR the bare name — the agent frequently passes just the
    name (e.g. "proton-enterprise") while nodes are keyed "name@ref"."""
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    nodes = [
        Node(id="v1", repo="proj@v3.3.1", kind="function", name="set_log_level",
             qualified_name="s.set_log_level", file_path="s.cpp", line_start=1,
             line_end=2, summary="", community="c", visibility="internal"),
    ]
    upsert_graph(client, prefix, nodes, [], datetime.now(timezone.utc))
    kg = KnowledgeGraph(client, stream_prefix=prefix)
    _eventually(lambda: len(kg.search_entities("set_log_level")), lambda v: v == 1)

    assert {h["id"] for h in kg.search_entities("set_log_level", repos=["proj@v3.3.1"])} == {"v1"}
    assert {h["id"] for h in kg.search_entities("set_log_level", repos=["proj"])} == {"v1"}  # bare name
    assert kg.search_entities("set_log_level", repos=["other"]) == []  # unrelated name -> nothing


def test_read_source_accepts_bare_name_or_entry_key(tp, tmp_path: Path):
    """read_source resolves a repo arg given either the full entry key or the
    bare name (matched to the single entry key by its name part)."""
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    (tmp_path / "s.cpp").write_text("l1\nl2\nl3\n")
    kg = KnowledgeGraph(client, stream_prefix=prefix, repo_paths={"proj@v3.3.1": tmp_path})

    assert kg.read_source("proj@v3.3.1", "s.cpp", 1, 2) == "l1\nl2\n"  # full key
    assert kg.read_source("proj", "s.cpp", 1, 2) == "l1\nl2\n"          # bare name
    with pytest.raises(ValueError):
        kg.read_source("nope", "s.cpp", 1, 2)


def test_read_source_bare_name_ambiguous_across_refs(tp, tmp_path: Path):
    """A bare name matching two refs is ambiguous -> raise, so the agent must
    retry with an explicit name@ref."""
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    (tmp_path / "a.txt").write_text("x\n")
    kg = KnowledgeGraph(client, stream_prefix=prefix,
                        repo_paths={"proj@v1": tmp_path, "proj@v2": tmp_path})
    with pytest.raises(ValueError):
        kg.read_source("proj", "a.txt", 1, 1)
    assert kg.read_source("proj@v1", "a.txt", 1, 1) == "x\n"  # explicit key still works


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


def _ids(result):
    return [p["id"] for p in result["path"] if "id" in p]


def test_path_between_finds_a_directed_call_chain(kg):
    # a1 -calls-> b1 -calls-> c1
    r = kg.path_between("a1", "c1")
    assert (r["found"], r["mode"], r["direction"]) == (True, "calls", "a_to_b")
    assert _ids(r) == ["a1", "b1", "c1"]
    assert [p["rel"] for p in r["path"] if "rel" in p] == ["calls", "calls"]


def test_path_between_reports_a_call_chain_running_the_other_way(kg):
    # Asked c1 -> a1, but it is a1 that (transitively) calls c1. Say so, and
    # list the chain caller-first -- never pretend c1 calls a1.
    r = kg.path_between("c1", "a1")
    assert (r["found"], r["mode"], r["direction"]) == (True, "calls", "b_to_a")
    assert _ids(r) == ["a1", "b1", "c1"]


def test_path_between_falls_back_to_a_related_path_and_says_so(kg):
    # d1 -documents-> a1 -calls-> ... : a real connection, but not a call chain.
    r = kg.path_between("d1", "c1")
    assert (r["found"], r["mode"]) == (True, "related")
    assert _ids(r)[0] == "d1" and _ids(r)[-1] == "c1"
    assert "not a call chain" in r["note"].lower()


def test_path_between_calls_mode_does_not_fall_back(kg):
    r = kg.path_between("d1", "c1", mode="calls")
    assert r["found"] is False and r["path"] == []
    # ...and gives the caller somewhere to go next
    assert [n["id"] for n in r["callers_of_b"]] == ["b1"]
    assert r["callees_of_a"] == []


def test_path_between_unknown_endpoint(kg):
    r = kg.path_between("a1", "missing")
    assert r["found"] is False
    assert [n["id"] for n in r["callees_of_a"]] == ["b1"]


def test_path_between_max_depth_is_clamped(kg):
    # max_depth=99 must be clamped to MAX_PATH_DEPTH rather than accepted
    # verbatim, and the search must still find the path within the clamp.
    assert kg.path_between("a1", "c1", max_depth=99)["found"] is True


def test_path_between_clamp_can_make_target_unreachable(kg, monkeypatch):
    from tpk.tools import KnowledgeGraph

    # a1 -> c1 needs 2 hops; clamping MAX_PATH_DEPTH to 1 must make it
    # unreachable even though the caller asked for max_depth=99.
    monkeypatch.setattr(KnowledgeGraph, "MAX_PATH_DEPTH", 1)
    r = kg.path_between("a1", "c1", max_depth=99, mode="calls")
    assert r["found"] is False and "depth" in r["note"]


def _add(kg, client_prefix, nodes, edges):
    from tpk.ingest import upsert_graph

    client, prefix = client_prefix
    upsert_graph(client, prefix, nodes, edges, datetime.now(timezone.utc))
    want = {n.id for n in nodes}
    _eventually(lambda: {r["id"] for r in kg._nodes_by_ids(sorted(want))}, lambda got: got == want)
    _eventually(lambda: len(kg._edges_touching(sorted(want), None, "both", None)),
                lambda n: n >= len(edges))


def _fn(id_, name, kind="function"):
    return Node(id=id_, repo="r1", kind=kind, name=name, qualified_name=f"x.{name}",
                file_path="x.py", line_start=1, line_end=2, summary="", community="0",
                visibility="internal")


def test_path_between_is_not_cut_off_by_a_wide_fan_out(kg, tp):
    """The old BFS kept `sorted(frontier)[:200]` per hop -- an arbitrary cut by
    id that hid real paths (#17). A caller with 250 callees must still reach the
    one that matters, even when its id sorts last."""
    fan = [_fn(f"fan{i:03d}", f"helper{i}") for i in range(250)]
    nodes = [_fn("hub_caller", "dispatch"), _fn("zzz_last", "theOneThatMatters"), _fn("goal", "goal")] + fan
    edges = ([Edge("hub_caller", n.id, "calls", "EXTRACTED", "r1") for n in fan]
             + [Edge("hub_caller", "zzz_last", "calls", "EXTRACTED", "r1"),
                Edge("zzz_last", "goal", "calls", "EXTRACTED", "r1")])
    _add(kg, tp, nodes, edges)
    r = kg.path_between("hub_caller", "goal", mode="calls")
    assert r["found"] is True and _ids(r) == ["hub_caller", "zzz_last", "goal"]


def test_related_path_never_routes_through_a_shared_symbol_or_file(kg, tp):
    """`x.cpp -imports-> Context <-imports- y.cpp` connects everything to
    everything and means nothing. Symbols and files may be endpoints, never
    the bridge."""
    # `left` also has two ordinary callees, so after the first hop the forward
    # frontier is the bigger one and the search expands from `right` next --
    # both sides then reach the connector, which must NOT count as a meeting.
    nodes = [_fn("left", "leftFn"), _fn("right", "rightFn"),
             _fn("left_c1", "leftCallee1"), _fn("left_c2", "leftCallee2"),
             _fn("sym_string", "String", kind="symbol"), _fn("file_ctx", "Context.h", kind="file")]
    edges = [Edge("left", "left_c1", "calls", "EXTRACTED", "r1"),
             Edge("left", "left_c2", "calls", "EXTRACTED", "r1"),
             Edge("left", "sym_string", "references", "EXTRACTED", "r1"),
             Edge("right", "sym_string", "references", "EXTRACTED", "r1"),
             Edge("left", "file_ctx", "imports", "EXTRACTED", "r1"),
             Edge("right", "file_ctx", "imports", "EXTRACTED", "r1")]
    _add(kg, tp, nodes, edges)
    assert kg.path_between("left", "right")["found"] is False
    # a symbol is still reachable as an ENDPOINT
    assert kg.path_between("left", "sym_string")["found"] is True


def test_path_between_stops_at_the_visited_budget(kg, tp, monkeypatch):
    from tpk.tools import KnowledgeGraph

    fan = [_fn(f"bud{i:03d}", f"b{i}") for i in range(60)]
    nodes = [_fn("bud_src", "src"), _fn("bud_dst", "dst")] + fan
    edges = [Edge("bud_src", n.id, "calls", "EXTRACTED", "r1") for n in fan]
    _add(kg, tp, nodes, edges)
    monkeypatch.setattr(KnowledgeGraph, "MAX_PATH_VISITED", 20)
    r = kg.path_between("bud_src", "bud_dst", mode="calls")
    assert r["found"] is False and "budget" in r["note"]


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

    assert kg.path_between("b1", "ghost1", max_depth=3)["found"] is False


def test_list_communities(kg):
    out = kg.list_communities(repo="r1")
    by_name = {r["community"]: r["node_count"] for r in out["communities"]}
    assert by_name == {"core": 2, "docs": 1}
    assert (out["total"], out["returned"], out["truncated"]) == (2, 2, False)
    # a single-repo call needs no per-repo orientation summary
    assert "by_repo" not in out


def test_list_communities_is_bounded_and_says_so(kg):
    """The result is capped (#76): largest first, and the caller can tell it
    was cut off and how much there is in total."""
    out = kg.list_communities(limit=1)
    assert [(r["repo"], r["community"]) for r in out["communities"]] == [("r1", "core")]
    assert (out["total"], out["returned"], out["truncated"]) == (3, 1, True)


def test_list_communities_limit_is_clamped(kg, monkeypatch):
    monkeypatch.setattr(type(kg), "MAX_COMMUNITIES", 2)
    assert kg.list_communities(limit=10_000)["returned"] == 2
    assert kg.list_communities(limit=0)["returned"] == 1   # floor of 1, not "unbounded"
    assert kg.list_communities()["returned"] == 2          # default also obeys the cap


def test_list_communities_min_nodes_drops_the_long_tail(kg):
    out = kg.list_communities(min_nodes=2)
    assert [(r["repo"], r["community"]) for r in out["communities"]] == [("r1", "core")]
    # total counts what matches the filter, so truncated stays meaningful
    assert (out["total"], out["truncated"]) == (1, False)


def test_list_communities_by_repo_orients_an_unfiltered_call(kg):
    out = kg.list_communities()
    assert {r["repo"]: (r["communities"], r["nodes"]) for r in out["by_repo"]} == {
        "r1": (2, 3), "docs": (1, 1)}


def test_list_communities_labels_say_what_a_cluster_is(kg):
    """Community ids are opaque integers from graphify; the label (dominant
    directories + files) is what makes the overview usable."""
    by_key = {(r["repo"], r["community"]): r for r in kg.list_communities()["communities"]}
    assert by_key[("docs", "docs")]["top_dirs"] == ["ops"]
    assert by_key[("docs", "docs")]["top_files"] == ["ckpt.md"]
    # root-level files have no directory; they still show up as files
    assert by_key[("r1", "core")]["top_dirs"] == []
    assert by_key[("r1", "core")]["top_files"] == ["m.py"]


def test_search_entities_limit_is_clamped(kg, monkeypatch):
    monkeypatch.setattr(type(kg), "MAX_SEARCH_RESULTS", 2)
    assert len(kg.search_entities("checkpoint", limit=10_000)) == 2
    assert len(kg.search_entities("checkpoint", limit=0)) == 1


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


def test_read_source_missing_file_raises_value_error(kg):
    """A guessed/nonexistent file path must surface as a ValueError (so the
    agent boundary can turn it into a structured error string) instead of a
    raw FileNotFoundError leaking a local absolute path."""
    with pytest.raises(ValueError, match="file not found in r1"):
        kg.read_source("r1", "does-not-exist.py", 1, 2)


def test_search_entities_concurrent_calls_succeed(kg):
    """Live-DB reproduction of the exact failure Task 7 hit: parallel tool
    calls sharing one KnowledgeGraph/timeplus_connect client used to fail
    with "Attempt to execute concurrent queries within the same session."
    The lock in KnowledgeGraph._query_rows must make this safe.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _search():
        return kg.search_entities("checkpoint flush")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_search) for _ in range(4)]
        results = [f.result() for f in futures]  # raises if any thread failed

    for hits in results:
        assert [h["id"] for h in hits] == ["a1"]


def test_read_source_output_is_capped(kg):
    from tpk.tools import KnowledgeGraph

    root = kg.repo_paths["r1"]
    (root / "big.py").write_text("".join(f"line{i}\n" for i in range(1, 501)))
    text = kg.read_source("r1", "big.py", 1, 500)
    lines = text.splitlines()
    assert len(lines) == KnowledgeGraph.MAX_SOURCE_LINES == 400
    assert lines[0] == "line1" and lines[-1] == "line400"


def test_search_falls_back_to_ranked_any_token_match(kg):
    # No entity matches all three tokens, but "checkpoint" and "flush" match
    # a1 (2 tokens) and "checkpoint" matches b1/d1 (1 token) — fallback must
    # return ranked results instead of nothing.
    hits = kg.search_entities("checkpoint flush zebra")
    assert hits, "fallback should return ranked partial matches"
    assert hits[0]["id"] == "a1"  # most tokens matched ranks first


def test_search_no_token_matches_still_empty(kg):
    assert kg.search_entities("xyzzy plugh") == []


def _seed_corpus_entry(client, prefix, name, ref, enabled):
    from tpk import corpus
    from tpk.config import RepoConfig

    corpus.upsert_entry(
        client,
        RepoConfig(name=name, github=f"org/{name}", ref=ref, visibility="internal",
                   enabled=enabled),
        prefix=prefix,
    )


def test_disabled_entry_invisible_everywhere(tp, tmp_path):
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    # two versions of one repo in the graph
    n1 = replace(SEED_NODES[0], id="v1n", repo="r@v1")
    n2 = replace(SEED_NODES[0], id="v2n", repo="r@v2")
    upsert_graph(client, prefix, [n1, n2], [], datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "r", "v1", enabled=True)
    _seed_corpus_entry(client, prefix, "r", "v2", enabled=False)

    kg = KnowledgeGraph(client, stream_prefix=prefix, corpus_ttl=0)
    _eventually(lambda: len(kg.search_entities("checkpoint")), lambda v: v >= 1)
    hits = kg.search_entities("checkpoint")
    assert {h["repo"] for h in hits} == {"r@v1"}          # search filtered
    assert kg.get_entity("v2n") is None                    # id lookup filtered
    assert kg.get_entity("v1n") is not None

    from tpk import corpus
    corpus.set_enabled(client, "r", "v2", True, prefix=prefix)
    _eventually(lambda: kg.get_entity("v2n"), lambda v: v is not None)  # live toggle


def test_all_entries_disabled_means_zero_results(tp, tmp_path):
    """Every corpus entry disabled -> `active` is `[]` (not `None`), so the
    `active or ["__none__"]` sentinel branch in `_nodes_by_ids` /
    `_edges_touching` / `search_entities` / `list_communities` must match
    nothing, not fall through to unfiltered."""
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    n1 = replace(SEED_NODES[0], id="allv1n", repo="allr@v1")
    upsert_graph(client, prefix, [n1], [], datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "allr", "v1", enabled=False)

    kg = KnowledgeGraph(client, stream_prefix=prefix, corpus_ttl=0)
    # Wait for the row to actually be visible before asserting on absence --
    # otherwise a slow write (not the filter) could make this pass for the
    # wrong reason.
    _eventually(lambda: len(corpus_entries_for(client, prefix)), lambda v: v >= 1)
    assert kg.search_entities("checkpoint") == []
    empty = kg.list_communities()
    assert (empty["communities"], empty["total"], empty["by_repo"]) == ([], 0, [])


def corpus_entries_for(client, prefix):
    from tpk import corpus

    return corpus.list_entries(client, prefix=prefix)


def test_empty_corpus_store_means_no_filter(kg):
    # the kg fixture seeds nodes but never touches kg_repos -> unfiltered
    assert kg.search_entities("checkpoint")


def test_role_scope_restricts_results(tp, tmp_path):
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph, ROLE_SCOPE

    client, prefix = tp
    docs_node = replace(SEED_NODES[0], id="rsdocsn", repo="docs@main")
    internal_node = replace(SEED_NODES[0], id="rsintn", repo="internal@v1")
    upsert_graph(client, prefix, [docs_node, internal_node], [], datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "docs", "main", enabled=True)
    _seed_corpus_entry(client, prefix, "internal", "v1", enabled=True)

    kg = KnowledgeGraph(client, stream_prefix=prefix, corpus_ttl=0)
    _eventually(lambda: len(kg.search_entities("checkpoint")), lambda v: v >= 2)

    token = ROLE_SCOPE.set(frozenset({"docs@main"}))
    try:
        hits = kg.search_entities("checkpoint")
        assert {h["repo"] for h in hits} == {"docs@main"}
        out = kg.list_communities()
        assert {r["repo"] for r in out["communities"]} == {"docs@main"}
        # the orientation summary must not leak out-of-scope repos either
        assert {r["repo"] for r in out["by_repo"]} == {"docs@main"}
    finally:
        ROLE_SCOPE.reset(token)

    # after reset: both repos visible again
    assert {h["repo"] for h in kg.search_entities("checkpoint")} == {"docs@main", "internal@v1"}


def test_role_scope_empty_intersection_matches_nothing(tp):
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph, ROLE_SCOPE

    client, prefix = tp
    docs_node = replace(SEED_NODES[0], id="eidocsn", repo="docs@main")
    upsert_graph(client, prefix, [docs_node], [], datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "docs", "main", enabled=True)

    kg = KnowledgeGraph(client, stream_prefix=prefix, corpus_ttl=0)
    _eventually(lambda: len(kg.search_entities("checkpoint")), lambda v: v >= 1)

    token = ROLE_SCOPE.set(frozenset({"absent@v9"}))
    try:
        assert kg.search_entities("checkpoint") == []
        assert kg.list_communities()["communities"] == []
        assert kg.list_communities()["by_repo"] == []
    finally:
        ROLE_SCOPE.reset(token)


def test_role_scope_applies_without_corpus_store(kg):
    from tpk.ingest import upsert_graph
    from tpk.tools import ROLE_SCOPE

    # kg fixture never touches kg_repos -> corpus state is None (unfiltered
    # by corpus); scope alone must still filter.
    docs_node = replace(SEED_NODES[0], id="nsdocsn", repo="docs@main")
    internal_node = replace(SEED_NODES[0], id="nsintn", repo="internal@v1")
    upsert_graph(kg.client, kg.prefix, [docs_node, internal_node], [], datetime.now(timezone.utc))
    _eventually(lambda: len(kg.search_entities("checkpoint")), lambda v: v >= 5)

    token = ROLE_SCOPE.set(frozenset({"docs@main"}))
    try:
        hits = kg.search_entities("checkpoint")
        assert {h["repo"] for h in hits} == {"docs@main"}
        # read_source must refuse a repo outside scope, even a static
        # repo_paths entry that predates any corpus/role concept.
        with pytest.raises(ValueError):
            kg.read_source("r1", "m.py", 1, 2)
    finally:
        ROLE_SCOPE.reset(token)

    # after reset: read_source against the static repo works again
    assert kg.read_source("r1", "m.py", 1, 2) == "line1\nline2\n"
