from pathlib import Path

from tpk.graphify_runner import parse_graph_json
from tpk.model import node_id

FIXTURE = Path(__file__).parent / "fixtures" / "sample_graph.json"


def test_parse_produces_stable_ids_and_edges():
    nodes, edges = parse_graph_json(FIXTURE, repo="tinyrepo", default_visibility="internal")
    assert len(nodes) == 6  # shape: real graphify output for tinyrepo (--code-only)
    assert len(edges) == 7  # shape: real graphify output for tinyrepo (--code-only)
    names = {n.name for n in nodes}
    assert {"main", "add"} <= names
    main = next(n for n in nodes if n.name == "main")
    assert main.id == node_id("tinyrepo", main.kind, main.qualified_name)
    assert main.repo == "tinyrepo"
    assert main.visibility == "internal"
    assert main.kind == "function"
    # every edge endpoint resolves to a parsed node id
    ids = {n.id for n in nodes}
    assert all(e.src in ids and e.dst in ids for e in edges)
    assert all(e.confidence in ("EXTRACTED", "INFERRED") for e in edges)
    # a calls-like edge from main to add is present
    add = next(n for n in nodes if n.name == "add")
    assert any(e.src == main.id and e.dst == add.id and e.rel == "calls" for e in edges)


def test_parse_drops_edges_with_unknown_endpoints(tmp_path: Path):
    bad = tmp_path / "graph.json"
    bad.write_text(
        '{"nodes": [{"id": "n1", "label": "x", "file_type": "code", "source_file": "a.py"}],'
        ' "links": [{"source": "n1", "target": "ghost", "relation": "calls"}]}'
    )
    nodes, edges = parse_graph_json(bad, repo="r", default_visibility="internal")
    assert len(nodes) == 1
    assert edges == []
