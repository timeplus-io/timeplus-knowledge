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
    # A second repo, seeded with the same old timestamp, that this test never
    # re-ingests: it must survive the stale-delete below, proving the delete
    # is scoped per-repo rather than a global "updated_at < t1" sweep.
    other_nodes = [_node("o1", "other_alpha", repo="other_repo")]
    upsert_graph(client, prefix, nodes + other_nodes, edges, run_started_at=t0)
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 2
    ) == 2
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "other_repo"), lambda v: v == 1
    ) == 1

    # second run: n2 disappeared, n1 updated -> n2 must be stale-deleted.
    # other_repo is untouched by this call -- upsert_graph derives `repos`
    # from this (tinyrepo-only) batch, so other_repo's old rows must remain.
    t1 = datetime.now(timezone.utc) + timedelta(seconds=1)
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], run_started_at=t1)
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 1
    ) == 1
    assert _eventually(
        lambda: _count(client, prefix, "kg_edges", "tinyrepo"), lambda v: v == 0
    ) == 0
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "other_repo"), lambda v: v == 1
    ) == 1


def test_upsert_graph_deletes_stale_rows_with_explicit_repos_and_empty_batch(tp):
    """A repo whose parse yields zero nodes/edges (e.g. the repo went fully
    empty) must still have its prior rows stale-deleted. Without an explicit
    `repos` set, upsert_graph would derive repos from the (empty) batch and
    never issue the delete -- this is what ingest_repo relies on by always
    passing `repos={repo_cfg.name}` explicitly.
    """
    from tpk.ingest import upsert_graph

    client, prefix = tp
    t0 = datetime.now(timezone.utc)
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], run_started_at=t0)
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 1
    ) == 1

    t1 = datetime.now(timezone.utc) + timedelta(seconds=1)
    upsert_graph(client, prefix, [], [], run_started_at=t1, repos={"tinyrepo"})
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 0
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


def test_ingest_repo_mid_upsert_failure_leaves_prior_rows_and_logs(tp, monkeypatch, tmp_path: Path):
    """graphify+parse succeed and the insert goes through, but the
    stale-delete itself raises. Prior rows must survive (new rows from the
    partial insert existing is acceptable -- the guarantee is prior rows are
    never lost) and the run must still be logged as failed.
    """
    from tpk import ingest as ingest_mod
    from tpk.ingest import ingest_repo, upsert_graph

    client, prefix = tp
    upsert_graph(
        client, prefix, [_node("n1", "alpha")], [], datetime.now(timezone.utc), repos={"tinyrepo"}
    )
    assert _eventually(
        lambda: _count(client, prefix, "kg_nodes", "tinyrepo"), lambda v: v == 1
    ) == 1

    # Stub graphify+parse so ingest_repo reaches upsert_graph's insert step.
    monkeypatch.setattr(ingest_mod, "run_graphify", lambda repo_path, out_dir: Path("unused"))
    monkeypatch.setattr(
        ingest_mod,
        "parse_graph_json",
        lambda path, repo, default_visibility: ([_node("n2", "gamma")], []),
    )

    # Let the insert succeed but make the stale-delete (client.command) blow up.
    orig_command = client.command

    def failing_command(sql, *args, **kwargs):
        if "DELETE" in sql:
            raise RuntimeError("stale-delete exploded")
        return orig_command(sql, *args, **kwargs)

    monkeypatch.setattr(client, "command", failing_command)

    cfg = RepoConfig(name="tinyrepo", path=tmp_path, visibility="internal")
    result = ingest_repo(client, cfg, prefix=prefix, out_root=tmp_path / "out")
    assert result.status == "failed"

    rows = _eventually(
        lambda: client.query(
            f"SELECT id FROM table({prefix}kg_nodes) WHERE repo = 'tinyrepo'"
        ).result_rows,
        lambda rows: len(rows) >= 1,
    )
    ids = {r[0] for r in rows}
    assert "n1" in ids  # prior row survived the failed run

    def _logs():
        return client.query(
            f"SELECT status FROM table({prefix}kg_ingest_log) WHERE repo = 'tinyrepo'"
        ).result_rows

    logs = _eventually(_logs, lambda rows: ("failed",) in rows)
    assert ("failed",) in logs


def test_ingest_repo_passes_extraction_and_backend(tp, monkeypatch, tmp_path: Path):
    from tpk import ingest as ingest_mod
    from tpk.ingest import ingest_repo

    client, prefix = tp
    calls: dict = {}

    def fake_run_graphify(repo_path, out_dir, extraction="code-only", backend=None):
        calls["extraction"] = extraction
        calls["backend"] = backend
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(ingest_mod, "run_graphify", fake_run_graphify)
    cfg = RepoConfig(
        name="r", path=tmp_path, visibility="internal", extraction="semantic"
    )
    result = ingest_repo(client, cfg, prefix=prefix, out_root=tmp_path / "o", backend="openai")
    assert result.status == "failed"  # capture stub raised, isolation held
    assert calls == {"extraction": "semantic", "backend": "openai"}
