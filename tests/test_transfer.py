"""Corpus export/import round-trip (issue #29)."""

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    # kg_ingest_log is append-only and takes ~2s to become visible via table();
    # wait so an immediately-following export actually captures the row (a real
    # export runs long after ingest, so this lag is a test artifact only).
    _wait_count(client, prefix, "kg_ingest_log", 1)


def _count(client, prefix, stream):
    return int(client.query(f"SELECT count() FROM table({prefix}{stream})").result_rows[0][0])


def _wait_count(client, prefix, stream, want, timeout=10.0):
    """Poll a count to `want`. Append-only streams (kg_ingest_log) have a
    short propagation lag before inserted rows are visible via table()."""
    import time
    deadline = time.monotonic() + timeout
    while _count(client, prefix, stream) != want and time.monotonic() < deadline:
        time.sleep(0.1)
    return _count(client, prefix, stream)


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
            want = _count(client, prefix, stream)
            assert _wait_count(client, dst, stream, want) == want, stream
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
        # kg_ingest_log is append-only (no primary key): a plain re-import
        # APPENDS its rows rather than upserting. Documented behavior — pinned
        # here so it's visible, not hidden.
        assert _wait_count(client, dst, "kg_ingest_log", 2) == 2  # 1 bundle row x 2 imports
        # --replace resets every stream to exactly the bundle's rows.
        transfer.import_bundle(client, bundle, prefix=dst, replace=True)
        assert _wait_count(client, dst, "kg_ingest_log", 1) == 1
    finally:
        db.drop_schema(client, dst)


def test_native_format_roundtrip(tp, tmp_path):
    """The --format Native path round-trips too (different wire encoding)."""
    client, prefix = tp
    _seed(client, prefix)
    bundle = tmp_path / "native"
    manifest = transfer.export_bundle(client, bundle, prefix=prefix, fmt="Native")
    assert manifest["format"] == "Native"
    assert (bundle / "kg_nodes.native").exists()

    dst = f"test_imp_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, dst)
    try:
        transfer.import_bundle(client, bundle, prefix=dst)
        for stream in transfer.GRAPH_STREAMS:
            want = _count(client, prefix, stream)
            assert _wait_count(client, dst, stream, want) == want, stream
        row = client.query(
            f"SELECT name FROM table({dst}kg_nodes) WHERE id = 'n1'").result_rows
        assert row == [("fn1",)]
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


# -- single-file transport + path stripping (#102) --------------------------

def _write_bundle(tmp_path, files=None, manifest=None):
    """A fake on-disk bundle for archive tests (no DB needed)."""
    b = tmp_path / "b"
    b.mkdir()
    files = files if files is not None else {"kg_nodes.parquet": b"nodes", "kg_edges.parquet": b"edges"}
    for name, data in files.items():
        (b / name).write_bytes(data)
    if manifest is None:
        manifest = {"bundle_version": 1, "format": "Parquet", "created_at": "x",
                    "streams": {n.split(".")[0]: {"file": n, "columns": [], "rows": 0} for n in files}}
    (b / transfer.MANIFEST_NAME).write_text(json.dumps(manifest))
    return b


def test_pack_unpack_roundtrip(tmp_path):
    b = _write_bundle(tmp_path)
    archive = transfer.pack_bundle(b, tmp_path / "c.tar.gz")
    out = tmp_path / "out"
    manifest = transfer.unpack_bundle(archive, out)
    assert set(manifest["streams"]) == {"kg_nodes", "kg_edges"}
    assert (out / "kg_nodes.parquet").read_bytes() == b"nodes"
    assert (out / "kg_edges.parquet").read_bytes() == b"edges"
    assert json.loads((out / transfer.MANIFEST_NAME).read_text()) == manifest


def _tar(path, members):
    """members: list of (TarInfo, bytes|None)."""
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            if data is None:
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return path


def _file(name, data):
    ti = tarfile.TarInfo(name)
    return ti, data


def test_unpack_rejects_path_traversal(tmp_path):
    manifest = json.dumps({"streams": {"x": {"file": "../evil", "columns": [], "rows": 0}}}).encode()
    archive = _tar(tmp_path / "t.tar.gz", [_file("manifest.json", manifest), _file("../evil", b"x")])
    with pytest.raises(transfer.BadBundle, match="unsafe name"):
        transfer.unpack_bundle(archive, tmp_path / "out")
    assert not (tmp_path / "evil").exists()
    assert list((tmp_path / "out").iterdir()) == []


def test_unpack_rejects_symlink_and_dirs(tmp_path):
    manifest = json.dumps({"streams": {}}).encode()
    link = tarfile.TarInfo("kg_nodes.parquet")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    archive = _tar(tmp_path / "t.tar.gz", [_file("manifest.json", manifest), (link, None)])
    with pytest.raises(transfer.BadBundle, match="not a regular file"):
        transfer.unpack_bundle(archive, tmp_path / "out")


def test_unpack_rejects_unlisted_and_missing_files(tmp_path):
    manifest = json.dumps({"streams": {"kg_nodes": {"file": "kg_nodes.parquet", "columns": [], "rows": 0}}}).encode()
    archive = _tar(tmp_path / "extra.tar.gz", [_file("manifest.json", manifest),
                                              _file("kg_nodes.parquet", b"n"), _file("stray.bin", b"s")])
    with pytest.raises(transfer.BadBundle, match="does not list"):
        transfer.unpack_bundle(archive, tmp_path / "out1")
    assert list((tmp_path / "out1").iterdir()) == []
    archive = _tar(tmp_path / "missing.tar.gz", [_file("manifest.json", manifest)])
    with pytest.raises(transfer.BadBundle, match="missing from the archive"):
        transfer.unpack_bundle(archive, tmp_path / "out2")


def test_unpack_rejects_no_manifest_and_garbage(tmp_path):
    archive = _tar(tmp_path / "t.tar.gz", [_file("kg_nodes.parquet", b"n")])
    with pytest.raises(transfer.BadBundle, match="no manifest"):
        transfer.unpack_bundle(archive, tmp_path / "out")
    garbage = tmp_path / "g.tar.gz"
    garbage.write_bytes(b"this is not an archive")
    with pytest.raises(transfer.BadBundle, match="not a gzipped tar"):
        transfer.unpack_bundle(garbage, tmp_path / "out2")


def test_export_strips_local_path_when_github_set(tp, tmp_path):
    client, prefix = tp
    corpus.upsert_entry(client, RepoConfig(name="gh", ref="v1", github="org/gh", path=Path("/home/me/gh")),
                        prefix=prefix)
    corpus.upsert_entry(client, RepoConfig(name="local", ref="", path=Path("/home/me/local")), prefix=prefix)
    _wait_count(client, prefix, "kg_repos", 2)
    manifest = transfer.export_bundle(client, tmp_path / "bundle", prefix=prefix, streams=["kg_repos"])
    assert manifest["streams"]["kg_repos"]["rows"] == 2

    client.command(f"DROP STREAM IF EXISTS {prefix}kg_repos")
    db.ensure_schema(client, prefix)
    transfer.import_bundle(client, tmp_path / "bundle", prefix=prefix)
    _wait_count(client, prefix, "kg_repos", 2)
    entries = {e.name: e for e in corpus.list_entries(client, prefix=prefix)}
    assert entries["gh"].path is None          # github entry: path dropped
    assert entries["local"].path == Path("/home/me/local")   # dev-mode entry keeps its path
