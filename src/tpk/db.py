"""Timeplus client factory and knowledge-graph schema DDL."""

import timeplus_connect

from tpk.config import Settings


def get_client(settings: Settings):
    return timeplus_connect.get_client(
        host=settings.host,
        port=settings.port,
        username=settings.user,
        password=settings.password,
    )


def ensure_schema(client, prefix: str = "") -> None:
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_nodes (
          id string,
          repo string,
          kind string,
          name string,
          qualified_name string,
          file_path string,
          line_start uint32,
          line_end uint32,
          summary string,
          community string,
          visibility string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (id)
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_edges (
          src string,
          dst string,
          rel string,
          confidence string,
          repo string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (src, dst, rel)
    """)
    client.command(f"""
        CREATE STREAM IF NOT EXISTS {prefix}kg_ingest_log (
          repo string,
          run_id string,
          nodes uint64,
          edges uint64,
          git_sha string,
          status string
        )
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_repos (
          name string,
          ref string,
          github string,
          path string,
          enabled bool,
          visibility string,
          extraction string,
          description string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (name, ref)
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_users (
          username string,
          password_hash string,
          role string,
          must_change_password bool,
          disabled bool,
          created_at datetime64(3, 'UTC'),
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (username)
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_roles (
          name string,
          entry_keys string,
          description string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (name)
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_sessions (
          token_hash string,
          username string,
          expires_at datetime64(3, 'UTC'),
          created_at datetime64(3, 'UTC')
        ) PRIMARY KEY (token_hash)
    """)


def drop_schema(client, prefix: str) -> None:
    if not prefix:
        raise ValueError("refusing to drop unprefixed (production) streams")
    for name in (
        "kg_nodes", "kg_edges", "kg_ingest_log", "kg_repos",
        "kg_users", "kg_roles", "kg_sessions",
    ):
        client.command(f"DROP STREAM IF EXISTS {prefix}{name}")
