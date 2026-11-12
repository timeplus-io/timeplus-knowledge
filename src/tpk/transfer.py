"""Corpus export/import (issue #29): dump an ingested graph to a portable
bundle and load it into another environment, skipping re-ingest.

Leverages proton/ClickHouse's own `FORMAT Parquet` (or `Native`) serialization
through the client's raw stream/insert — the engine encodes/decodes the
files, so there is no Python-side type juggling and no extra dependency.

A bundle is a directory: one file per stream plus a `manifest.json`. For
transport through the web UI (#102) a bundle is packed into a single flat
`.tar.gz` (`pack_bundle` / `unpack_bundle`); the on-disk layout is the same
either way, so `tpk import` reads an unpacked archive unchanged.

Both directions take an optional entry filter (#104): a list of entry keys
(`name@ref`, or bare `name` for local-path entries). Every graph stream
carries the key (`repo` on nodes/edges/ingest_log, `name`+`ref` on
kg_repos), so a partial export is a WHERE on each stream's dump and a partial
import stages the bundle in throwaway streams and copies the matching rows.
"""

import json
import shutil
import tarfile
import time
import uuid
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
FORMATS = tuple(_EXT)
ARCHIVE_SUFFIX = ".tar.gz"

# A `path` in `kg_repos` is a checkout on the EXPORTING host's filesystem and
# means nothing after a cross-environment import (#102). When the entry also
# names a GitHub source the target can re-fetch from, the bundle carries an
# empty path so the Manage tab doesn't show a dead local path.
_EXPORT_EXPR = {
    ("kg_repos", "path"): "if(github != '', '', path) AS path",
}

# SQL for a row's entry key, per stream -- the same identity
# `config.entry_key` computes in Python (`name@ref`, or `name` alone when the
# entry has no ref).
_KEY_EXPR = {"kg_repos": "if(ref != '', concat(name, '@', ref), name)"}


def _key_expr(stream: str) -> str:
    return _KEY_EXPR.get(stream, "repo")


def _entry_filter(stream: str, entries) -> tuple[str, dict]:
    """(`WHERE ...` clause, query parameters) selecting rows of `entries`;
    ('', {}) when `entries` is None (no filter)."""
    if entries is None:
        return "", {}
    keys = list(entries)
    if not keys:
        # `has([], x)` has no element type to infer; an empty selection is
        # simply no rows.
        return " WHERE 0", {}
    return f" WHERE has(%(entries)s, {_key_expr(stream)})", {"entries": keys}


def list_entry_keys(client, prefix: str = "") -> list[str]:
    """Every entry key the corpus registry currently holds, sorted."""
    rows = client.query(
        f"SELECT {_key_expr('kg_repos')} FROM {db.latest(db.qualified('kg_repos', prefix))}"
    ).result_rows
    return sorted(r[0] for r in rows)


# Append-only streams have no key: their row ORDER is the data (ingest.py
# picks the newest `kg_ingest_log` row per entry by `_tp_time`). Insert time
# is the only ordering, so the bundle must carry `_tp_time` for these, or
# every imported row lands on one timestamp and `started`/`ok` become
# indistinguishable (an imported entry then shows "started", later "stale").
# Keyed streams are upserted by primary key and need no such column.
TIME_ORDERED_STREAMS = frozenset({"kg_ingest_log"})


def _stream_columns(client, stream: str, prefix: str = "") -> list[str]:
    """Real, insertable columns of a stream — excludes proton's internal
    `_tp_*` pseudo-columns (except `_tp_time` on time-ordered streams, which
    is insertable and carries the row order) and any ALIAS/MATERIALIZED
    columns."""
    rows = client.query(f"DESCRIBE {db.qualified(stream, prefix)}").result_rows
    keep_time = stream in TIME_ORDERED_STREAMS
    cols = []
    for row in rows:
        name = row[0]
        default_type = row[2] if len(row) > 2 else ""
        if name == "_tp_time" and keep_time:
            cols.append(name)
            continue
        if name.startswith("_tp") or default_type in ("ALIAS", "MATERIALIZED"):
            continue
        cols.append(name)
    return cols


def _row_count(client, stream: str, prefix: str = "", entries=None) -> int:
    where, params = _entry_filter(stream, entries)
    r = client.query(f"SELECT count() FROM table({db.qualified(stream, prefix)}){where}",
                     parameters=params).result_rows
    return int(r[0][0]) if r else 0


def export_bundle(client, out_dir, streams=None, prefix: str = "",
                  fmt: str = "Parquet", entries=None) -> dict:
    """Stream each graph/corpus stream to `<out_dir>/<stream>.<ext>` in the
    engine's `fmt` serialization and write a manifest. Returns the manifest.

    `entries` (#104): keep only rows of these entry keys. The manifest always
    records which entries the bundle holds (`entries`, from the registry
    rows it carries) and whether it was filtered (`partial`)."""
    streams = list(streams) if streams is not None else list(GRAPH_STREAMS)
    ext = _EXT.get(fmt, fmt.lower())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if entries is not None:
        entries = sorted(set(entries))
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "format": fmt,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "partial": entries is not None,
        "entries": [],
        "streams": {},
    }
    for stream in streams:
        cols = _stream_columns(client, stream, prefix=prefix)
        fname = f"{stream}.{ext}"
        # raw_stream returns the engine's serialized bytes as a file-like
        # object; copy it out in chunks so a 300k-row stream never lands
        # wholly in memory.
        select = [_EXPORT_EXPR.get((stream, c), c) for c in cols]
        where, params = _entry_filter(stream, entries)
        src = client.raw_stream(
            f"SELECT {', '.join(select)} FROM table({db.qualified(stream, prefix)}){where}",
            parameters=params, fmt=fmt)
        with open(out / fname, "wb") as fh:
            shutil.copyfileobj(src, fh)
        manifest["streams"][stream] = {
            "file": fname,
            "columns": cols,
            "rows": _row_count(client, stream, prefix=prefix, entries=entries),
        }
    if "kg_repos" in streams:
        keys = list_entry_keys(client, prefix=prefix)
        manifest["entries"] = [k for k in keys if entries is None or k in entries]
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(in_dir) -> dict:
    return json.loads((Path(in_dir) / MANIFEST_NAME).read_text())


def purge_entries(client, entries, prefix: str = "") -> None:
    """Delete the registry row, nodes and edges of each entry key (what the
    Manage tab's delete-with-purge does, for a list of keys). The append-only
    ingest log keeps its history rows: there is no DELETE on it."""
    keys = list(entries)
    if not keys:
        return
    params = {"entries": keys}
    db.delete(client, db.qualified("kg_repos", prefix),
              f"has(%(entries)s, {_key_expr('kg_repos')})", params, ("name", "ref"))
    db.delete(client, db.qualified("kg_nodes", prefix), "has(%(entries)s, repo)", params, ("id",))
    db.delete(client, db.qualified("kg_edges", prefix), "has(%(entries)s, repo)", params,
              ("src", "dst", "rel"))


def _wait_rows(client, stream: str, prefix: str, want: int, timeout: float = 60.0) -> None:
    """Block until `table(stream)` shows at least `want` rows. Keyed streams
    have a sub-second write->read lag and the append-only log a ~2s one; a
    staged load must be fully visible before it is copied out."""
    deadline = time.monotonic() + timeout
    while True:
        have = _row_count(client, stream, prefix=prefix)
        if have >= want:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{stream}: staged {have} of {want} bundle rows became visible within {timeout:.0f}s")
        time.sleep(0.2)


def import_bundle(client, in_dir, prefix: str = "", replace: bool = False,
                  entries=None) -> dict:
    """Load a bundle into the current environment. Runs `ensure_schema` first
    (so a fresh env gets the streams). Returns the manifest.

    Idempotency: `kg_nodes`/`kg_edges`/`kg_repos` are mutable, primary-keyed
    streams, so a plain load is an idempotent upsert. `kg_ingest_log` is
    append-only (no key), so a plain re-import APPENDS its provenance rows --
    harmless for `tpk status` (which takes arg_max per repo) but not deduped.
    Use `replace` to reset every target stream to exactly the bundle's rows.

    `entries` (#104): load only rows of these entry keys. The bundle files
    are then loaded into staging streams under a throwaway prefix and the
    matching rows copied into the targets engine-side, so no Python-side
    Parquet decoding is needed. With `entries`, `replace` means "replace
    THOSE entries": their existing registry row, nodes and edges are purged
    first; the other entries, and all streams, are left alone.

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
                f"{stream}: target stream is missing column(s) {missing} -- "
                f"schema mismatch between the bundle and this deployment")

    if entries is not None:
        loaded = _import_entries(client, src, manifest, prefix, replace, sorted(set(entries)))
        # Report what was actually copied, not the bundle's totals: a job or
        # CLI run that loaded one entry of a ten-entry bundle should say so.
        out = dict(manifest, loaded_entries=sorted(set(entries)))
        out["streams"] = {stream: dict(info, rows=loaded[stream])
                          for stream, info in manifest["streams"].items()}
        return out

    for stream, info in manifest["streams"].items():
        if replace:
            # `replace` clears stale rows not present in the bundle. Drop +
            # recreate (synchronous DDL) rather than TRUNCATE, whose async
            # apply on a mutable stream can race -- and wipe -- the load that
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


def _import_entries(client, src: Path, manifest: dict, prefix: str, replace: bool,
                    entries: list[str]) -> dict[str, int]:
    """Partial load (#104): stage every bundled stream under a throwaway
    prefix, wait for the staged rows to be readable, then copy the selected
    entries' rows into the targets. The targets are not touched until all
    staging succeeded, so a failed stage leaves the graph as it was. Returns
    the number of rows copied per stream."""
    fmt = manifest.get("format", "Parquet")
    stage = f"{prefix}imp{uuid.uuid4().hex[:8]}_"
    db.ensure_graph_streams(client, stage)
    loaded: dict[str, int] = {}
    try:
        for stream, info in manifest["streams"].items():
            with open(src / info["file"], "rb") as fh:
                client.raw_insert(table=db.qualified(stream, stage),
                                  column_names=info["columns"],
                                  insert_block=fh, fmt=fmt)
            _wait_rows(client, stream, stage, info["rows"])
        if replace:
            purge_entries(client, entries, prefix=prefix)
        for stream, info in manifest["streams"].items():
            cols = ", ".join(info["columns"])
            # Keyed staging streams read through latest() (hides proton
            # tombstones); the append-only log reads table() so `_tp_time`
            # comes through with the row.
            source = (f"table({db.qualified(stream, stage)})" if stream in TIME_ORDERED_STREAMS
                      else db.latest(db.qualified(stream, stage)))
            where, params = _entry_filter(stream, entries)
            client.command(
                f"INSERT INTO {db.qualified(stream, prefix)} ({cols}) "
                f"SELECT {cols} FROM {source}{where}", parameters=params)
            loaded[stream] = int(client.query(
                f"SELECT count() FROM {source}{where}", parameters=params).result_rows[0][0])
    finally:
        db.drop_graph_streams(client, stage)
    return loaded


# -- single-file transport (#102) -------------------------------------------

def pack_bundle(bundle_dir, archive_path) -> Path:
    """Tar+gzip a bundle directory into one flat archive (manifest first,
    then exactly the files the manifest lists). Returns the archive path."""
    src = Path(bundle_dir)
    manifest = load_manifest(src)
    out = Path(archive_path)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src / MANIFEST_NAME, arcname=MANIFEST_NAME)
        for info in manifest["streams"].values():
            tar.add(src / info["file"], arcname=info["file"])
    return out


class BadBundle(ValueError):
    """The archive is not a bundle this version will load."""


def _check_member(m: tarfile.TarInfo):
    # Flat archive of regular files only: no directories, links, devices,
    # nested paths or `..` -- every member lands directly in the dest dir.
    if not m.isfile():
        raise BadBundle(f"archive member {m.name!r} is not a regular file")
    name = m.name
    if name != Path(name).name or name in (".", "..") or name.startswith("."):
        raise BadBundle(f"archive member {m.name!r} has an unsafe name")


def unpack_bundle(archive_path, dest_dir, max_members: int = 64) -> dict:
    """Extract a `pack_bundle` archive into `dest_dir`, validating every
    member BEFORE writing anything: flat regular files only, a readable
    `manifest.json`, and no file the manifest doesn't list. Returns the
    manifest. Raises `BadBundle` on anything else."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        tar = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BadBundle(f"not a gzipped tar archive: {exc}") from exc
    with tar:
        members = tar.getmembers()
        if len(members) > max_members:
            raise BadBundle(f"archive has {len(members)} members (max {max_members})")
        for m in members:
            _check_member(m)
        names = [m.name for m in members]
        if MANIFEST_NAME not in names:
            raise BadBundle(f"archive has no {MANIFEST_NAME}")
        raw = tar.extractfile(MANIFEST_NAME).read()
        try:
            manifest = json.loads(raw)
            listed = {info["file"] for info in manifest["streams"].values()}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BadBundle(f"malformed {MANIFEST_NAME}: {exc}") from exc
        extra = sorted(set(names) - listed - {MANIFEST_NAME})
        if extra:
            raise BadBundle(f"archive contains files the manifest does not list: {extra}")
        missing = sorted(listed - set(names))
        if missing:
            raise BadBundle(f"manifest lists files missing from the archive: {missing}")
        for m in members:
            with tar.extractfile(m) as src, open(dest / m.name, "wb") as fh:
                shutil.copyfileobj(src, fh)
    return manifest
