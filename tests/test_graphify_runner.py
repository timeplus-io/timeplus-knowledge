import json
from pathlib import Path

import pytest

import tpk.graphify_runner as graphify_runner
from tpk.graphify_runner import GraphifyError, parse_graph_json, run_graphify
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


# graphify's documented file_type enum is exactly {code, document, paper,
# image, rationale, concept} (graphify/skills/claude/references/
# extraction-spec.md, shipped in the installed graphifyy package). Only
# "code" is source-derived; the rest are always doc-ish. Its documented
# confidence enum is {EXTRACTED, INFERRED, AMBIGUOUS}. These tests use those
# documented values directly (no LLM run required to observe them).


def test_doc_kind_node_is_public_even_with_internal_default(tmp_path: Path):
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "n1", "label": "Concept From Docs", "file_type": "document", "source_file": "README.md"},
                    {"id": "n2", "label": "helper.py", "file_type": "code", "source_file": "helper.py"},
                ],
                "links": [
                    {"source": "n1", "target": "n2", "relation": "references", "confidence": "EXTRACTED"},
                ],
            }
        )
    )
    nodes, edges = parse_graph_json(g, repo="r", default_visibility="internal")
    doc_node = next(n for n in nodes if n.kind == "document")
    code_node = next(n for n in nodes if n.kind == "file")
    assert doc_node.visibility == "public"
    assert code_node.visibility == "internal"


def test_ambiguous_confidence_maps_to_inferred(tmp_path: Path):
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "a", "label": "a.py", "file_type": "code", "source_file": "a.py"},
                    {"id": "b", "label": "b.py", "file_type": "code", "source_file": "b.py"},
                ],
                "links": [
                    {"source": "a", "target": "b", "relation": "conceptually_related_to", "confidence": "AMBIGUOUS"},
                ],
            }
        )
    )
    _, edges = parse_graph_json(g, repo="r", default_visibility="internal")
    assert len(edges) == 1
    assert edges[0].confidence == "INFERRED"


def test_inferred_confidence_stays_inferred(tmp_path: Path):
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "a", "label": "a.py", "file_type": "code", "source_file": "a.py"},
                    {"id": "b", "label": "b.py", "file_type": "code", "source_file": "b.py"},
                ],
                "links": [
                    {"source": "a", "target": "b", "relation": "semantically_similar_to", "confidence": "INFERRED"},
                ],
            }
        )
    )
    _, edges = parse_graph_json(g, repo="r", default_visibility="internal")
    assert len(edges) == 1
    assert edges[0].confidence == "INFERRED"


def test_file_kind_nodes_in_same_file_get_distinct_ids(tmp_path: Path):
    # Real graphify output maps every non-callable "code" node to kind="file"
    # -- not just the one node representing the file itself. Two such nodes
    # in the same source_file (e.g. two top-level structs, or a shell
    # function the `_callable` heuristic missed) must not collapse onto the
    # same node id, or the mutable-stream upsert silently drops one of them.
    # Reproduces the shape seen in real timeplus-cli output: a genuine file
    # node plus another non-callable "code" node, same source_file, same
    # line.
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "common", "label": "common.go", "file_type": "code",
                     "source_file": "internal/common.go", "source_location": "L1"},
                    {"id": "common_topology", "label": "Topology", "file_type": "code",
                     "source_file": "internal/common.go", "source_location": "L1"},
                ],
                "links": [],
            }
        )
    )
    nodes, _ = parse_graph_json(g, repo="r", default_visibility="internal")
    assert len(nodes) == 2
    assert all(n.kind == "file" for n in nodes)
    ids = {n.id for n in nodes}
    assert len(ids) == 2, "two distinct file-kind nodes in one file must not share an id"
    qualified_names = {n.qualified_name for n in nodes}
    assert len(qualified_names) == 2


def test_run_graphify_missing_binary_raises_graphify_error(tmp_path: Path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'graphify'")

    monkeypatch.setattr(graphify_runner.subprocess, "run", fake_run)
    with pytest.raises(GraphifyError, match="graphify executable not found"):
        run_graphify(tmp_path / "repo", tmp_path / "out")


class _FakeProc:
    returncode = 0
    stderr = ""


def _capture_graphify(monkeypatch, calls):
    """Fake subprocess.run that records argv and fabricates graph.json."""
    import tpk.graphify_runner as gr

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        gj = out / "graphify-out" / "graph.json"
        gj.parent.mkdir(parents=True, exist_ok=True)
        gj.write_text('{"nodes": [], "links": []}')
        return _FakeProc()

    monkeypatch.setattr(gr.subprocess, "run", fake_run)


def test_run_graphify_code_only_argv(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "out")
    assert "--code-only" in calls[0]
    assert "--backend" not in calls[0]


def test_run_graphify_semantic_argv_with_backends(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "o1", extraction="semantic")
    assert "--code-only" not in calls[0]
    assert "--backend" not in calls[0]  # auto: let graphify detect

    run_graphify(tmp_path, tmp_path / "o2", extraction="semantic", backend="claude")
    assert calls[1][calls[1].index("--backend") + 1] == "claude"

    run_graphify(tmp_path, tmp_path / "o3", extraction="semantic", backend="openai")
    assert calls[2][calls[2].index("--backend") + 1] == "openai"


def test_run_graphify_semantic_fails_fast_without_keys(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import GraphifyError, run_graphify

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list = []
    _capture_graphify(monkeypatch, calls)

    with pytest.raises(GraphifyError, match="ANTHROPIC_API_KEY or OPENAI_API_KEY"):
        run_graphify(tmp_path, tmp_path / "out", extraction="semantic")
    with pytest.raises(GraphifyError, match="ANTHROPIC_API_KEY"):
        run_graphify(tmp_path, tmp_path / "out", extraction="semantic", backend="claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(GraphifyError, match="OPENAI_API_KEY"):
        run_graphify(tmp_path, tmp_path / "out", extraction="semantic", backend="openai")
    assert calls == []  # never reached subprocess
