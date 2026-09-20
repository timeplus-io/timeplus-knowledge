"""Timeplus client factory and knowledge-graph schema DDL.

Two DB backends are supported (issue #50), selected by TPK_DB_BACKEND. These
name a *stream-semantics mode*, not strictly a server product:
- "timeplusd" (default) — keyed state lives in MUTABLE STREAMs (upsert by PK,
  real DELETE, latest-state table() reads). MUTABLE STREAM is a Timeplus
  Enterprise (timeplusd) feature, so this mode requires Enterprise.
- "proton" — no MUTABLE STREAM. Keyed state uses `versioned_kv` streams (upsert
  by PK, table() = latest-per-key) plus a `deleted` tombstone column; DELETE
  becomes a tombstone upsert and reads filter `deleted = 0`. `versioned_kv` is
  in OSS proton *and* Enterprise (which supports the full proton feature set),
  so this mode runs on BOTH — it's the OSS-compatible mode, not proton-only.

The per-backend differences are localized to three helpers here — keyed-stream
DDL (in ensure_schema), `latest()` (read source), and `delete()` — so call
sites elsewhere stay backend-agnostic. Plain inserts are identical on both
(the `deleted` column defaults to 0), and the two append-only streams
(kg_ingest_log, chat_audit_log) are unchanged.
"""

import logging
import time

import timeplus_connect
from timeplus_connect.driver.httputil import default_pool_manager

from tpk.config import Settings, database, db_backend

logger = logging.getLogger(__name__)


def qualified(name: str, prefix: str = "") -> str:
    """Fully-qualified identifier for a tpk stream: `<database>.<prefix><name>`.

    All tpk streams live under the configured database (default `tpk`, #58), so
    every DDL/DML/read site builds its stream name through here. `prefix`
    (TPK_STREAM_PREFIX) still namespaces within the database, e.g. for tests."""
    return f"{database()}.{prefix}{name}"


def _keyed_stream(prefix: str, name: str, columns: list[str], pk: str) -> str:
    """DDL for a keyed (upsertable, latest-state) stream, per backend."""
    cols = ",\n          ".join(columns)
    stream = qualified(name, prefix)
    if db_backend() == "proton":
        return (
            f"CREATE STREAM IF NOT EXISTS {stream} (\n"
            f"          {cols},\n"
            f"          deleted uint8 DEFAULT 0\n"
            f"        ) PRIMARY KEY ({pk}) SETTINGS mode='versioned_kv'"
        )
    return (
        f"CREATE MUTABLE STREAM IF NOT EXISTS {stream} (\n"
        f"          {cols}\n"
        f"        ) PRIMARY KEY ({pk})"
    )


def latest(stream: str) -> str:
    """FROM-source SQL for a latest-state read of a keyed stream (full name,
    prefix included). On proton, wraps table() to hide soft-deleted rows."""
    if db_backend() == "proton":
        return f"(SELECT * FROM table({stream}) WHERE deleted = 0)"
    return f"table({stream})"


def delete(client, stream: str, where: str, parameters: dict, pk: tuple[str, ...]) -> None:
    """Delete rows from a keyed stream. Real DELETE on timeplusd; on proton
    (versioned_kv has no DELETE) tombstone the matching live rows by upserting
    their primary keys with deleted=1, which reads then filter out."""
    if db_backend() != "proton":
        client.command(f"DELETE FROM {stream} WHERE {where}", parameters=parameters)
        return
    pk_cols = ", ".join(pk)
    rows = client.query(
        f"SELECT {pk_cols} FROM table({stream}) WHERE deleted = 0 AND ({where})",
        parameters=parameters,
    ).result_rows
    if not rows:
        return
    client.insert(stream, [[*r, 1] for r in rows], column_names=[*pk, "deleted"])


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def get_client(settings: Settings):
    kwargs = dict(
        host=settings.host,
        port=settings.port,
        username=settings.user,
        password=settings.password,
    )
    # A shell-wide HTTP_PROXY would otherwise send requests for a LOCAL
    # timeplusd through the proxy (502s; an MCP client just sees "Connection
    # closed", #77). The driver only consults the proxy env vars when it has
    # to pick a pool manager itself, so hand it the plain one.
    if settings.host.lower() in _LOOPBACK_HOSTS:
        kwargs["pool_mgr"] = default_pool_manager()
    return timeplus_connect.get_client(**kwargs)


def connect_with_retry(settings: Settings, timeout_s: float = 60.0,
                       interval_s: float = 2.0):
    """Return a client, waiting up to `timeout_s` for timeplusd to accept
    connections.

    On `docker compose up` the agent's eager `tpk serve` bootstrap can start
    before timeplusd is listening on its HTTP port (compose `depends_on` only
    waits for the container to start, not for the DB to be ready). Without
    this, the first connection raises `Connection refused` and the process
    exits, recovering only via a container restart — a noisy crash-loop on
    every startup. Retrying makes startup wait for the DB instead.
    """
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return get_client(settings)
        except Exception as exc:  # connection refused, DNS not ready, etc.
            if time.monotonic() >= deadline:
                raise
            logger.warning(
                "timeplusd not ready (attempt %d: %s); retrying in %.0fs",
                attempt, type(exc).__name__, interval_s,
            )
            time.sleep(interval_s)


def _add_column_if_missing(client, stream: str, column: str, col_type: str) -> None:
    """Idempotently add a column to an existing mutable stream. Safe to call
    on every startup: if the column is already present the ALTER is a no-op
    (IF NOT EXISTS), and any residual engine error is swallowed so schema
    setup never fails on an already-migrated stream."""
    try:
        client.command(
            f"ALTER STREAM {stream} ADD COLUMN IF NOT EXISTS {column} {col_type}"
        )
    except Exception:
        # Column already exists (older engines without IF NOT EXISTS support)
        # or a benign race with another starting process — either way the
        # column is present, which is all we need.
        pass


def ensure_schema(client, prefix: str = "") -> None:
    # All tpk streams live under a dedicated database (default `tpk`, #58);
    # create it first so the qualified DDL below resolves. CREATE DATABASE is a
    # global statement (works from the default-database session get_client uses)
    # and is a no-op if it already exists, on both timeplusd and proton.
    client.command(f"CREATE DATABASE IF NOT EXISTS {database()}")
    # Keyed (upsertable, latest-state) streams -- MUTABLE on timeplusd,
    # versioned_kv + `deleted` tombstone on proton (see _keyed_stream).
    client.command(_keyed_stream(prefix, "kg_nodes", [
        "id string", "repo string", "kind string", "name string",
        "qualified_name string", "file_path string", "line_start uint32",
        "line_end uint32", "summary string", "community string",
        "visibility string", "updated_at datetime64(3, 'UTC')",
    ], pk="id"))
    client.command(_keyed_stream(prefix, "kg_edges", [
        "src string", "dst string", "rel string", "confidence string",
        "repo string", "updated_at datetime64(3, 'UTC')",
    ], pk="src, dst, rel"))
    client.command(f"""
        CREATE STREAM IF NOT EXISTS {qualified('kg_ingest_log', prefix)} (
          repo string,
          run_id string,
          nodes uint64,
          edges uint64,
          git_sha string,
          status string
        )
    """)
    client.command(_keyed_stream(prefix, "kg_repos", [
        "name string", "ref string", "github string", "path string",
        "enabled bool", "visibility string", "extraction string",
        "description string", "updated_at datetime64(3, 'UTC')",
    ], pk="name, ref"))
    client.command(_keyed_stream(prefix, "kg_users", [
        "username string", "password_hash string", "role string",
        "must_change_password bool", "disabled bool",
        "daily_token_limit uint32",
        "created_at datetime64(3, 'UTC')", "updated_at datetime64(3, 'UTC')",
    ], pk="username"))
    # Per-user daily token budget override (0 = inherit role/global), #62;
    # backfilled onto users created before it as 0.
    _add_column_if_missing(client, qualified("kg_users", prefix), "daily_token_limit", "uint32")
    client.command(_keyed_stream(prefix, "kg_roles", [
        "name string", "entry_keys string", "capabilities string",
        "description string", "daily_token_limit uint32",
        "updated_at datetime64(3, 'UTC')",
    ], pk="name"))
    # Backfill for deployments whose kg_roles predates the capabilities
    # column (CREATE ... IF NOT EXISTS won't add it to an existing stream).
    # Existing role rows keep an empty cell, which auth._parse_capabilities
    # reads as the chat-only migration default.
    _add_column_if_missing(client, qualified("kg_roles", prefix), "capabilities", "string")
    # Daily per-user token budget per role (0 = inherit the global fallback);
    # backfilled onto roles created before #62 as 0 (inherit).
    _add_column_if_missing(client, qualified("kg_roles", prefix), "daily_token_limit", "uint32")
    client.command(_keyed_stream(prefix, "kg_sessions", [
        "token_hash string", "username string",
        "expires_at datetime64(3, 'UTC')", "created_at datetime64(3, 'UTC')",
    ], pk="token_hash"))
    # Long-lived per-user API tokens for the remote MCP endpoint (#74). No
    # nullable columns in this schema: expires_at / last_used_at use the epoch
    # (1970-01-01) as the "never" sentinel -- see auth._NEVER. `last_used_at`
    # is written once at creation (as that sentinel) and NEVER updated: the
    # credential row is write-once so a last-use touch can't race a revoke and
    # re-insert (resurrect) it. Live last-use lives in kg_api_token_usage.
    client.command(_keyed_stream(prefix, "kg_api_tokens", [
        "token_hash string", "token_id string", "username string",
        "name string", "hint string",
        "created_at datetime64(3, 'UTC')", "expires_at datetime64(3, 'UTC')",
        "last_used_at datetime64(3, 'UTC')",
    ], pk="token_hash"))
    # Last-use timestamps for the above, keyed by the same hash. Separate so
    # the throttled touch only ever writes here: a usage row whose credential
    # row is gone grants nothing.
    client.command(_keyed_stream(prefix, "kg_api_token_usage", [
        "token_hash string", "last_used_at datetime64(3, 'UTC')",
    ], pk="token_hash"))
    # Append-only support-history audit log: one row per chat turn (question,
    # answer, and the tool calls made). Not MUTABLE -- like kg_ingest_log, we
    # want the full event history, not a keyed latest-state view.
    client.command(f"""
        CREATE STREAM IF NOT EXISTS {qualified('chat_audit_log', prefix)} (
          ts datetime64(3, 'UTC'),
          turn_id string,
          conversation_id string,
          username string,
          role string,
          provider string,
          model string,
          question string,
          answer string,
          history_len uint32,
          tool_calls string,
          sources string,
          tool_count uint32,
          source_count uint32,
          latency_ms uint32,
          status string,
          error string
        )
    """)
    # Append-only per-turn token usage, for daily-budget enforcement (#62).
    # Written independent of TPK_CHAT_AUDIT so a token budget can't be silently
    # disabled by turning auditing off.
    client.command(f"""
        CREATE STREAM IF NOT EXISTS {qualified('chat_usage', prefix)} (
          ts datetime64(3, 'UTC'),
          username string,
          tokens uint32
        )
    """)


def drop_schema(client, prefix: str) -> None:
    if not prefix:
        raise ValueError("refusing to drop unprefixed (production) streams")
    for name in (
        "kg_nodes", "kg_edges", "kg_ingest_log", "kg_repos",
        "kg_users", "kg_roles", "kg_sessions", "kg_api_tokens",
        "kg_api_token_usage", "chat_audit_log", "chat_usage",
    ):
        client.command(f"DROP STREAM IF EXISTS {qualified(name, prefix)}")
