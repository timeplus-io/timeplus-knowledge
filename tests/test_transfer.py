"""Corpus export/import round-trip (issue #29)."""

import uuid
from datetime import datetime, timezone

import pytest

from conftest import requires_timeplus
from tpk import corpus, db, transfer
from tpk.config import RepoConfig
from tpk.model import EDGE_COLUMNS, NODE_COLUMNS, Edge, Node, edge_rows, node_rows

pytestmark = requires_timeplus


def _seed(client, prefix):
    now = datetime.now(timezone.utc)
    nodes = [
        Node(f"n{i}", "demo@v1", "function", f"fn{i}", f"demo::fn{i}",
             "src/x.cpp", i, i + 5, f"summary {i}", "c0", "public")
        for i in range(3)
    ]
    edges = [Edge("n0", "n1", "calls", "EXTRACTED", "demo@v1"),
             Edge("n1", "n2", "calls", "INFERRED", "demo@v1")]
    client.insert(f"{prefix}kg_nodes", node_rows(nodes, now), column_names=NODE_COLUMNS)
    client.insert(f"{prefix}kg_edges", edge_rows(edges, now), column_names=EDGE_COLUMNS)
    corpus.upsert_entry(client, RepoConfig(name="demo", ref="v1", github="org/demo"),
                        prefix=prefix)
    client.insert(f"{prefix}kg_ingest_log",
                  [["demo@v1", "run-1", 3, 2, "abc (v1)", "ok"]],
                  column_names=["repo", "run_id", "nodes", "edges", "git_sha", "status"])


def _count(client, prefix, stream):
    return int(client.query(f"SELECT count() FROM table({prefix}{stream})").result_rows[0][0])


def test_export_import_roundtrip(tp, tmp_path):
    client, prefix = tp
    _seed(client, prefix)

    bundle = tmp_path / "bundle"
    manifest = transfer.export_bundle(client, bundle, prefix=prefix)

    assert manifest["format"] == "Parquet"
    assert manifest["streams"]["kg_nodes"]["rows"] == 3
    assert manifest["streams"]["kg_edges"]["rows"] == 2
    assert (bundle / "manifest.json").exists()
    assert (bundle / "kg_nodes.parquet").exists()

    dst = f"test_imp_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, dst)
    try:
        transfer.import_bundle(client, bundle, prefix=dst)
        for stream in transfer.GRAPH_STREAMS:
            assert _count(client, dst, stream) == _count(client, prefix, stream), stream
        # spot-check a node round-tripped faithfully
        row = client.query(
            f"SELECT name, qualified_name, line_start FROM table({dst}kg_nodes)"
            f" WHERE id = 'n1'").result_rows
        assert row == [("fn1", "demo::fn1", 1)]
    finally:
        db.drop_schema(client, dst)


def test_import_is_idempotent_upsert(tp, tmp_path):
    """Re-importing the mutable graph streams converges (keyed upsert), not
    doubles."""
    client, prefix = tp
    _seed(client, prefix)
    bundle = tmp_path / "b"
    transfer.export_bundle(client, bundle, prefix=prefix)

    dst = f"test_imp_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, dst)
    try:
        transfer.import_bundle(client, bundle, prefix=dst)
        transfer.import_bundle(client, bundle, prefix=dst)  # twice
        assert _count(client, dst, "kg_nodes") == 3
        assert _count(client, dst, "kg_edges") == 2
        assert _count(client, dst, "kg_repos") == 1
    finally:
        db.drop_schema(client, dst)


def test_replace_truncates_stale_rows(tp, tmp_path):
    client, prefix = tp
    _seed(client, prefix)
    bundle = tmp_path / "b"
    transfer.export_bundle(client, bundle, prefix=prefix)

    dst = f"test_imp_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, dst)
    try:
        # Pre-existing stale node not in the bundle.
        client.insert(f"{dst}kg_nodes",
                      node_rows([Node("stale", "old@v0", "function", "old", "old",
                                       "f", 1, 2, "s", "c", "public")],
                                datetime.now(timezone.utc)),
                      column_names=NODE_COLUMNS)
        transfer.import_bundle(client, bundle, prefix=dst, replace=True)
        assert _count(client, dst, "kg_nodes") == 3  # stale row gone
        assert client.query(
            f"SELECT count() FROM table({dst}kg_nodes) WHERE id = 'stale'"
        ).result_rows[0][0] == 0
    finally:
        db.drop_schema(client, dst)


def test_schema_mismatch_rejected(tp, tmp_path):
    client, prefix = tp
    _seed(client, prefix)
    bundle = tmp_path / "b"
    transfer.export_bundle(client, bundle, prefix=prefix)

    # Tamper the manifest to claim a column the target stream doesn't have.
    import json
    mpath = bundle / "manifest.json"
    m = json.loads(mpath.read_text())
    m["streams"]["kg_nodes"]["columns"].append("nonexistent_col")
    mpath.write_text(json.dumps(m))

    dst = f"test_imp_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, dst)
    try:
        with pytest.raises(ValueError, match="schema mismatch"):
            transfer.import_bundle(client, bundle, prefix=dst)
        # Nothing loaded — the validation runs before any insert.
        assert _count(client, dst, "kg_edges") == 0
    finally:
        db.drop_schema(client, dst)
