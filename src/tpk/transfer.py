"""Corpus export/import (issue #29): dump an ingested graph to a portable
bundle and load it into another environment, skipping re-ingest.

Leverages proton/ClickHouse's own `FORMAT Parquet` (or `Native`) serialization
through the client's raw stream/insert — the engine encodes/decodes the
files, so there is no Python-side type juggling and no extra dependency.

A bundle is a directory: one file per stream plus a `manifest.json`.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from tpk import db

# The expensive-to-produce, portable streams. Auth streams
# (kg_users/kg_roles/kg_sessions) are environment-specific and secret-bearing,
# so they are deliberately excluded.
GRAPH_STREAMS = ["kg_nodes", "kg_edges", "kg_repos", "kg_ingest_log"]

MANIFEST_NAME = "manifest.json"
BUNDLE_VERSION = 1
_EXT = {"Parquet": "parquet", "Native": "native"}


def _stream_columns(client, stream: str, prefix: str = "") -> list[str]:
    """Real, insertable columns of a stream — excludes proton's internal
    `_tp_*` pseudo-columns and any ALIAS/MATERIALIZED columns."""
    rows = client.query(f"DESCRIBE {db.qualified(stream, prefix)}").result_rows
    cols = []
    for row in rows:
        name = row[0]
        default_type = row[2] if len(row) > 2 else ""
        if name.startswith("_tp") or default_type in ("ALIAS", "MATERIALIZED"):
            continue
        cols.append(name)
    return cols


def _row_count(client, stream: str, prefix: str = "") -> int:
    r = client.query(f"SELECT count() FROM table({db.qualified(stream, prefix)})").result_rows
    return int(r[0][0]) if r else 0


def export_bundle(client, out_dir, streams=None, prefix: str = "",
                  fmt: str = "Parquet") -> dict:
    """Stream each graph/corpus stream to `<out_dir>/<stream>.<ext>` in the
    engine's `fmt` serialization and write a manifest. Returns the manifest."""
    streams = list(streams) if streams is not None else list(GRAPH_STREAMS)
    ext = _EXT.get(fmt, fmt.lower())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "format": fmt,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "streams": {},
    }
    for stream in streams:
        cols = _stream_columns(client, stream, prefix=prefix)
        fname = f"{stream}.{ext}"
        # raw_stream returns the engine's serialized bytes as a file-like
        # object; copy it out in chunks so a 300k-row stream never lands
        # wholly in memory.
        src = client.raw_stream(
            f"SELECT {', '.join(cols)} FROM table({db.qualified(stream, prefix)})", fmt=fmt)
        with open(out / fname, "wb") as fh:
            shutil.copyfileobj(src, fh)
        manifest["streams"][stream] = {
            "file": fname,
            "columns": cols,
            "rows": _row_count(client, stream, prefix=prefix),
        }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(in_dir) -> dict:
    return json.loads((Path(in_dir) / MANIFEST_NAME).read_text())


def import_bundle(client, in_dir, prefix: str = "", replace: bool = False) -> dict:
    """Load a bundle into the current environment. Runs `ensure_schema` first
    (so a fresh env gets the streams). Returns the manifest.

    Idempotency: `kg_nodes`/`kg_edges`/`kg_repos` are mutable, primary-keyed
    streams, so a plain load is an idempotent upsert. `kg_ingest_log` is
    append-only (no key), so a plain re-import APPENDS its provenance rows —
    harmless for `tpk status` (which takes arg_max per repo) but not deduped.
    Use `replace` to reset every target stream to exactly the bundle's rows.

    Rejects a bundle whose columns aren't all present in the target stream
    (schema drift between the exporting and importing versions) before writing
    anything."""
    src = Path(in_dir)
    manifest = load_manifest(src)
    fmt = manifest.get("format", "Parquet")
    db.ensure_schema(client, prefix)

    # Validate every bundled stream against the target BEFORE writing anything,
    # so a mismatch fails cleanly with nothing partially loaded.
    for stream, info in manifest["streams"].items():
        path = src / info["file"]
        if not path.exists():
            raise FileNotFoundError(f"bundle file missing: {path}")
        target_cols = set(_stream_columns(client, stream, prefix=prefix))
        missing = [c for c in info["columns"] if c not in target_cols]
        if missing:
            raise ValueError(
                f"{stream}: target stream is missing column(s) {missing} — "
                f"schema mismatch between the bundle and this deployment")

    for stream, info in manifest["streams"].items():
        if replace:
            # `replace` clears stale rows not present in the bundle. Drop +
            # recreate (synchronous DDL) rather than TRUNCATE, whose async
            # apply on a mutable stream can race — and wipe — the load that
            # follows. Done per stream, immediately before its load, so a
            # mid-run failure only affects the stream in flight rather than
            # leaving every earlier-dropped stream empty.
            client.command(f"DROP STREAM IF EXISTS {db.qualified(stream, prefix)}")
            db.ensure_schema(client, prefix)
        with open(src / info["file"], "rb") as fh:
            client.raw_insert(table=db.qualified(stream, prefix),
                              column_names=info["columns"],
                              insert_block=fh, fmt=fmt)
    return manifest
