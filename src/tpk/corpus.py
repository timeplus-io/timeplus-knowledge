"""The corpus store: kg_repos is the source of truth for what the graph
indexes. repos.toml seeds it; the management API and CLI mutate it."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tpk.config import RepoConfig, entry_key, load_repos, resolved_repo_path

_COLUMNS = [
    "name", "ref", "github", "path", "enabled", "visibility",
    "extraction", "description", "updated_at",
]


def _row(cfg: RepoConfig) -> list:
    return [
        cfg.name, cfg.ref, cfg.github, str(cfg.path) if cfg.path else "",
        cfg.enabled, cfg.visibility, cfg.extraction, cfg.description,
        datetime.now(timezone.utc),
    ]


def _to_cfg(row) -> RepoConfig:
    name, ref, github, path, enabled, visibility, extraction, description, _ = row
    return RepoConfig(
        name=name, ref=ref, github=github, path=Path(path) if path else None,
        enabled=bool(enabled), visibility=visibility, extraction=extraction,
        description=description,
    )


def upsert_entry(client, cfg: RepoConfig, prefix: str = "") -> None:
    client.insert(f"{prefix}kg_repos", [_row(cfg)], column_names=_COLUMNS)


def list_entries(client, prefix: str = "") -> list[RepoConfig]:
    rows = client.query(
        f"SELECT {', '.join(_COLUMNS)} FROM table({prefix}kg_repos)"
        " ORDER BY name, ref"
    ).result_rows
    return [_to_cfg(r) for r in rows]


def find_entry(client, name: str, ref: str, prefix: str = "") -> RepoConfig | None:
    rows = client.query(
        f"SELECT {', '.join(_COLUMNS)} FROM table({prefix}kg_repos)"
        " WHERE name = %(n)s AND ref = %(r)s",
        parameters={"n": name, "r": ref},
    ).result_rows
    return _to_cfg(rows[0]) if rows else None


def set_enabled(client, name: str, ref: str, enabled: bool, prefix: str = "") -> bool:
    cfg = find_entry(client, name, ref, prefix=prefix)
    if cfg is None:
        return False
    upsert_entry(client, replace(cfg, enabled=enabled), prefix=prefix)
    return True


def delete_entry(
    client, name: str, ref: str, prefix: str = "", purge: bool = False
) -> bool:
    cfg = find_entry(client, name, ref, prefix=prefix)
    if cfg is None:
        return False
    client.command(
        f"DELETE FROM {prefix}kg_repos WHERE name = %(n)s AND ref = %(r)s",
        parameters={"n": name, "r": ref},
    )
    if purge:
        key = entry_key(cfg)
        for stream in ("kg_nodes", "kg_edges"):
            client.command(
                f"DELETE FROM {prefix}{stream} WHERE repo = %(k)s",
                parameters={"k": key},
            )
    return True


def enabled_keys(client, prefix: str = "") -> list[str]:
    rows = client.query(
        f"SELECT name, ref FROM table({prefix}kg_repos) WHERE enabled"
        " ORDER BY name, ref"
    ).result_rows
    return [f"{n}@{r}" if r else n for n, r in rows]


def seed_from_toml(client, toml_path: Path, prefix: str = "") -> int:
    existing = {(e.name, e.ref) for e in list_entries(client, prefix=prefix)}
    added = 0
    for cfg in load_repos(toml_path).values():
        if (cfg.name, cfg.ref) not in existing:
            upsert_entry(client, cfg, prefix=prefix)
            added += 1
    return added


def entry_paths(entries: list[RepoConfig]) -> dict[str, Path]:
    return {entry_key(e): resolved_repo_path(e) for e in entries}


def enabled_keys_from(entries: list[RepoConfig]) -> list[str]:
    return [entry_key(e) for e in entries if e.enabled]
