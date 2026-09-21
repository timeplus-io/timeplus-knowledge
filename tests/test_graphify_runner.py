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
    # Real graphify output emits many non-callable "code" nodes per file, not
    # just the one node representing the file itself. Two such nodes
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
    # only the node that IS the file is a "file"; the struct is a "symbol" (#17)
    assert {n.name: n.kind for n in nodes} == {"common.go": "file", "Topology": "symbol"}
    ids = {n.id for n in nodes}
    assert len(ids) == 2, "two distinct file-kind nodes in one file must not share an id"
    qualified_names = {n.qualified_name for n in nodes}
    assert len(qualified_names) == 2


# -- kinds and names from graph structure (#17) -------------------------------
# graphify has no class/method kinds: a class is `_callable` + `_callable_class`,
# an in-class inline method is callable with a ".name()" label, and an
# OUT-OF-CLASS definition (`BlockIO Foo::execute() {...}` -- the dominant C++
# form) comes out NON-callable with a BARE label, merged into the header
# declaration. The shapes below are copied from real proton-enterprise output
# (src/Interpreters/InterpreterInsertQuery.{h,cpp}).

def _cpp_graph(tmp_path: Path) -> Path:
    H, CPP = "src/I/Insert.h", "src/I/Insert.cpp"
    code = {"file_type": "code"}
    g = tmp_path / "graph.json"
    g.write_text(json.dumps({
        "nodes": [
            {"id": "h", "label": "Insert.h", "source_file": H, "source_location": "L1", **code},
            {"id": "cpp", "label": "Insert.cpp", "source_file": CPP, "source_location": "L1", **code},
            {"id": "cls", "label": "InterpreterInsertQuery", "source_file": H, "source_location": "L20",
             "_callable": True, "_callable_class": True, **code},
            {"id": "base", "label": "IInterpreter", "source_file": H, "source_location": "L20", **code},
            {"id": "ctor", "label": "InterpreterInsertQuery::InterpreterInsertQuery()", "source_file": CPP,
             "source_location": "L46", "_callable": True, **code},
            {"id": "inline", "label": ".supportsTransactions()", "source_file": H, "source_location": "L66",
             "_callable": True, **code},
            {"id": "exec", "label": "execute", "source_file": H, "source_location": "L36", **code},
            {"id": "decl_only", "label": "getTable", "source_file": H, "source_location": "L63", **code},
            {"id": "field", "label": "query_ptr", "source_file": H, "source_location": "L71", **code},
            {"id": "free", "label": "isTrivialSelect()", "source_file": CPP, "source_location": "L183",
             "_callable": True, **code},
            {"id": "alias", "label": "String", "source_file": H, "source_location": "L5", **code},
        ],
        "links": [
            {"source": "h", "target": "cls", "relation": "contains"},
            {"source": "cls", "target": "base", "relation": "inherits"},
            {"source": "cls", "target": "inline", "relation": "method"},
            {"source": "cls", "target": "exec", "relation": "defines"},
            {"source": "cpp", "target": "exec", "relation": "contains"},
            {"source": "exec", "target": "free", "relation": "calls"},
            {"source": "cls", "target": "decl_only", "relation": "defines"},
            {"source": "cls", "target": "field", "relation": "defines"},
            {"source": "cpp", "target": "ctor", "relation": "contains"},
            {"source": "cpp", "target": "free", "relation": "contains"},
            {"source": "exec", "target": "alias", "relation": "references"},
        ],
    }))
    return g


def test_kinds_are_derived_from_graph_structure(tmp_path: Path):
    nodes, _ = parse_graph_json(_cpp_graph(tmp_path), repo="r", default_visibility="internal")
    assert {n.name: n.kind for n in nodes} == {
        "Insert.h": "file",
        "Insert.cpp": "file",
        "InterpreterInsertQuery": "class",
        "IInterpreter": "class",                                  # only known as a base class
        "InterpreterInsertQuery::InterpreterInsertQuery": "function",
        "InterpreterInsertQuery::supportsTransactions": "function",  # ".name()" inline method
        "InterpreterInsertQuery::execute": "function",            # out-of-class definition
        "InterpreterInsertQuery::getTable": "member",             # declared, body not in the graph
        "InterpreterInsertQuery::query_ptr": "member",            # field
        "isTrivialSelect": "function",
        "String": "symbol",                                       # bare type reference
    }


def test_out_of_class_method_keeps_its_calls_and_is_searchable_by_class(tmp_path: Path):
    nodes, edges = parse_graph_json(_cpp_graph(tmp_path), repo="r", default_visibility="internal")
    by_name = {n.name: n for n in nodes}
    execute = by_name["InterpreterInsertQuery::execute"]
    assert "InterpreterInsertQuery::execute" in execute.qualified_name
    calls = {(e.src, e.dst) for e in edges if e.rel == "calls"}
    assert (execute.id, by_name["isTrivialSelect"].id) in calls


def test_reclassified_nodes_keep_distinct_ids(tmp_path: Path):
    nodes, _ = parse_graph_json(_cpp_graph(tmp_path), repo="r", default_visibility="internal")
    assert len({n.id for n in nodes}) == len(nodes) == 11


def test_same_method_name_in_two_classes_does_not_collide(tmp_path: Path):
    g = tmp_path / "graph.json"
    code = {"file_type": "code", "source_file": "a.h", "source_location": "L1"}
    g.write_text(json.dumps({
        "nodes": [
            {"id": "A", "label": "A", "_callable": True, "_callable_class": True, **code},
            {"id": "B", "label": "B", "_callable": True, "_callable_class": True, **code},
            {"id": "a_exec", "label": "execute", **code},
            {"id": "b_exec", "label": "execute", **code},
        ],
        "links": [
            {"source": "A", "target": "a_exec", "relation": "defines"},
            {"source": "B", "target": "b_exec", "relation": "defines"},
            {"source": "a_exec", "target": "b_exec", "relation": "calls"},
        ],
    }))
    nodes, edges = parse_graph_json(g, repo="r", default_visibility="internal")
    assert sorted(n.name for n in nodes if n.kind == "function") == ["A::execute"]
    assert sorted(n.name for n in nodes if n.kind == "member") == ["B::execute"]
    assert len({n.id for n in nodes}) == 4 and len(edges) == 3


def test_overloaded_functions_in_one_file_keep_distinct_ids(tmp_path: Path):
    # Two overloads share `file::name`; the mutable-stream upsert would silently
    # keep only one (~3% of proton's functions). The second falls back to the
    # graphify id, so both survive and the first keeps its readable name.
    g = tmp_path / "graph.json"
    code = {"file_type": "code", "source_file": "a.cpp", "source_location": "L1", "_callable": True}
    g.write_text(json.dumps({"nodes": [{"id": "f_int", "label": "f()", **code},
                                       {"id": "f_str", "label": "f()", **code}], "links": []}))
    nodes, _ = parse_graph_json(g, repo="r", default_visibility="internal")
    assert [n.name for n in nodes] == ["f", "f"]
    assert sorted(n.qualified_name for n in nodes) == ["a.cpp::f", "a.cpp::f#f_str"]
    assert len({n.id for n in nodes}) == 2


class _FakePopen:
    """Stand-in for subprocess.Popen: yields canned stdout lines, fabricates
    graph.json under --out, and reports returncode."""
    def __init__(self, cmd, stdout=None, stderr=None, text=None, env=None, bufsize=None):
        self.cmd = cmd
        self.env = env
        self.returncode = 0
        out = Path(cmd[cmd.index("--out") + 1])
        gj = out / "graphify-out" / "graph.json"
        gj.parent.mkdir(parents=True, exist_ok=True)
        gj.write_text('{"nodes": [], "links": []}')
        self.stdout = iter(["extracting app.py\n", "extracting util.py\n"])
    def wait(self):
        return self.returncode


def _capture_graphify(monkeypatch, calls, popen_cls=_FakePopen):
    """Fake subprocess.Popen that records argv/env and fabricates graph.json."""
    import tpk.graphify_runner as gr

    def fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return popen_cls(cmd, **kwargs)

    monkeypatch.setattr(gr.subprocess, "Popen", fake_popen)


def test_run_graphify_missing_binary_raises_graphify_error(tmp_path: Path, monkeypatch):
    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'graphify'")
    monkeypatch.setattr(graphify_runner.subprocess, "Popen", fake_popen)
    with pytest.raises(GraphifyError, match="graphify executable not found"):
        run_graphify(tmp_path / "repo", tmp_path / "out")


def test_run_graphify_forwards_stdout_lines_and_sets_unbuffered(monkeypatch, tmp_path: Path):
    calls: list = []
    _capture_graphify(monkeypatch, calls)
    seen: list[str] = []
    run_graphify(tmp_path, tmp_path / "out", on_line=seen.append)
    assert seen == ["extracting app.py", "extracting util.py"]  # newline-stripped, in order
    assert calls[0]["env"]["PYTHONUNBUFFERED"] == "1"


def test_run_graphify_nonzero_exit_raises_with_output_tail(monkeypatch, tmp_path: Path):
    class _FailPopen(_FakePopen):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.returncode = 1
            self.stdout = iter(["boom line 1\n", "boom line 2\n"])
    calls: list = []
    _capture_graphify(monkeypatch, calls, popen_cls=_FailPopen)
    with pytest.raises(GraphifyError, match="boom line 2"):
        run_graphify(tmp_path, tmp_path / "out")


def test_run_graphify_code_only_argv(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "out")
    assert "--code-only" in calls[0]["cmd"]
    assert "--backend" not in calls[0]["cmd"]


def test_run_graphify_semantic_argv_with_backends(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "o1", extraction="semantic")
    assert "--code-only" not in calls[0]["cmd"]
    assert "--backend" not in calls[0]["cmd"]  # auto: let graphify detect

    run_graphify(tmp_path, tmp_path / "o2", extraction="semantic", backend="claude")
    assert calls[1]["cmd"][calls[1]["cmd"].index("--backend") + 1] == "claude"

    run_graphify(tmp_path, tmp_path / "o3", extraction="semantic", backend="openai")
    assert calls[2]["cmd"][calls[2]["cmd"].index("--backend") + 1] == "openai"


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


def test_run_graphify_semantic_model_flag(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(
        tmp_path, tmp_path / "o", extraction="semantic", backend="openai", model="gpt-5.2"
    )
    assert calls[0]["cmd"][calls[0]["cmd"].index("--model") + 1] == "gpt-5.2"


def test_run_graphify_semantic_accepts_base_url_instead_of_key(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    calls: list = []
    _capture_graphify(monkeypatch, calls)

    # a gateway URL (e.g. Bedrock behind LiteLLM) counts as a usable backend
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.internal/v1")
    run_graphify(tmp_path, tmp_path / "o1", extraction="semantic", backend="openai")
    run_graphify(tmp_path, tmp_path / "o2", extraction="semantic")  # auto
    assert len(calls) == 2
    # graphify hard-requires the key env var even for gateway endpoints, so
    # a placeholder must be injected into the child env
    assert calls[0]["env"]["OPENAI_API_KEY"] == "placeholder-gateway-key"
    # auto mode must infer --backend from the base URL (graphify's own
    # auto-detect only looks at API keys)
    assert calls[1]["cmd"][calls[1]["cmd"].index("--backend") + 1] == "openai"

    # both base URLs with no keys is ambiguous
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://other.internal")
    with pytest.raises(GraphifyError, match="disambiguate"):
        run_graphify(tmp_path, tmp_path / "o3", extraction="semantic")


def test_run_graphify_stream_mode(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import GraphifyError, run_graphify

    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "o1", stream=True)
    # With Popen, stream=True changes the behavior of on_line echoing to stdout

    # Handle failure case with Popen
    class _FailPopen(_FakePopen):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.returncode = 1

    import tpk.graphify_runner as gr
    calls = []
    _capture_graphify(monkeypatch, calls, popen_cls=_FailPopen)
    with pytest.raises(GraphifyError, match="graphify failed"):
        run_graphify(tmp_path, tmp_path / "o2", stream=True)


def test_run_graphify_token_budget_flag(monkeypatch, tmp_path: Path):
    from tpk.graphify_runner import run_graphify

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    calls: list = []
    _capture_graphify(monkeypatch, calls)
    run_graphify(tmp_path, tmp_path / "o1", extraction="semantic", token_budget=16000)
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--token-budget") + 1] == "16000"
    run_graphify(tmp_path, tmp_path / "o2", extraction="semantic")
    assert "--token-budget" not in calls[1]["cmd"]
    run_graphify(tmp_path, tmp_path / "o3", token_budget=16000)  # code-only: no flag
    assert "--token-budget" not in calls[2]["cmd"]
