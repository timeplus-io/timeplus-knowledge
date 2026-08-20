from pathlib import Path

import tpk.ingest as ingest_mod
from tpk.config import RepoConfig
from tpk.ingest import IngestProgress, ingest_repo
from tpk.model import Edge, Node


def test_ingest_repo_reports_phase_sequence(monkeypatch, tmp_path: Path):
    # Stub graphify to emit two heartbeat lines, and parse to a known graph.
    def fake_run_graphify(repo_path, out_dir, on_line=None, **kw):
        for ln in ("extracting a.py", "extracting b.py"):
            if on_line:
                on_line(ln)
        gj = tmp_path / "graph.json"
        gj.write_text("{}")
        return gj

    nodes = [Node(id="n1", repo="r", kind="function", name="f",
                  qualified_name="m.f", file_path="m.py", line_start=1, line_end=2,
                  summary="s", community="c", visibility="internal")]
    edges: list[Edge] = []
    monkeypatch.setattr(ingest_mod, "run_graphify", fake_run_graphify)
    monkeypatch.setattr(ingest_mod, "parse_graph_json", lambda *a, **k: (nodes, edges))
    monkeypatch.setattr(ingest_mod, "upsert_graph", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_git_sha", lambda *a, **k: "sha")

    events: list[IngestProgress] = []
    cfg = RepoConfig(name="r", path=tmp_path, ref="", visibility="internal", enabled=True)
    result = ingest_repo(object(), cfg, prefix="", out_root=tmp_path / "out",
                         on_progress=events.append)

    assert result.status == "ok" and result.nodes == 1 and result.edges == 0
    phases = [e.phase for e in events]
    # local-path repo -> no fetch; extract (incl. 2 heartbeats) -> parse -> upsert -> done
    assert phases[0] == "extract"
    assert phases.count("extract") >= 3          # start + 2 line heartbeats
    assert [p for p in ("parse", "upsert", "done") if p in phases] == ["parse", "upsert", "done"]
    assert [e.message for e in events if e.phase == "extract" and e.message] == \
        ["extracting a.py", "extracting b.py"]
    # counts are known from parse onward
    done = next(e for e in events if e.phase == "done")
    assert done.nodes == 1 and done.edges == 0


def test_ingest_repo_no_progress_callback_is_noop(monkeypatch, tmp_path: Path):
    # Default on_progress=None must not raise.
    monkeypatch.setattr(ingest_mod, "run_graphify", lambda *a, **k: tmp_path / "g.json")
    monkeypatch.setattr(ingest_mod, "parse_graph_json", lambda *a, **k: ([], []))
    monkeypatch.setattr(ingest_mod, "upsert_graph", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_git_sha", lambda *a, **k: "sha")
    cfg = RepoConfig(name="r", path=tmp_path, ref="", visibility="internal", enabled=True)
    assert ingest_repo(object(), cfg, out_root=tmp_path / "o").status == "ok"
