"""Per-repo ingest: graphify -> parse -> upsert -> stale-delete -> log."""

import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tpk.config import RepoConfig
from tpk.graphify_runner import parse_graph_json, run_graphify
from tpk.model import EDGE_COLUMNS, NODE_COLUMNS, Edge, Node, edge_rows, node_rows


@dataclass(frozen=True)
class IngestResult:
    repo: str
    run_id: str
    nodes: int
    edges: int
    status: str  # "ok" | "failed"


def upsert_graph(
    client,
    prefix: str,
    nodes: list[Node],
    edges: list[Edge],
    run_started_at: datetime,
    repos: set[str] | None = None,
) -> None:
    if nodes:
        client.insert(f"{prefix}kg_nodes", node_rows(nodes, run_started_at), column_names=NODE_COLUMNS)
    if edges:
        client.insert(f"{prefix}kg_edges", edge_rows(edges, run_started_at), column_names=EDGE_COLUMNS)
    if repos is None:
        # Derived from the batch: callers seeding multi-repo test data rely on
        # this. Note this means a repo whose parse yielded zero nodes/edges
        # would derive to an empty set here and its stale rows would never be
        # deleted -- ingest_repo avoids that by always passing `repos`
        # explicitly.
        repos = {n.repo for n in nodes} | {e.repo for e in edges}
    for repo in repos:
        for stream in ("kg_nodes", "kg_edges"):
            client.command(
                f"DELETE FROM {prefix}{stream} WHERE repo = %(r)s AND updated_at < %(t)s",
                parameters={"r": repo, "t": run_started_at},
            )


def _git_sha(repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"], capture_output=True, text=True
        )
    except FileNotFoundError:  # no git binary (e.g. minimal container)
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _log(client, prefix: str, result: IngestResult, git_sha: str) -> None:
    client.insert(
        f"{prefix}kg_ingest_log",
        [[result.repo, result.run_id, result.nodes, result.edges, git_sha, result.status]],
        column_names=["repo", "run_id", "nodes", "edges", "git_sha", "status"],
    )


def ingest_repo(
    client,
    repo_cfg: RepoConfig,
    prefix: str = "",
    out_root: Path = Path(".graphify_out"),
    backend: str | None = None,
    model: str | None = None,
    token_budget: int = 0,
    stream: bool = False,
) -> IngestResult:
    run_id = uuid.uuid4().hex[:12]
    run_started_at = datetime.now(timezone.utc)
    try:
        graph_json = run_graphify(
            repo_cfg.path,
            out_root / repo_cfg.name,
            extraction=repo_cfg.extraction,
            backend=backend,
            model=model,
            token_budget=token_budget,
            stream=stream,
        )
        nodes, edges = parse_graph_json(graph_json, repo_cfg.name, repo_cfg.visibility)
        upsert_graph(client, prefix, nodes, edges, run_started_at, repos={repo_cfg.name})
        result = IngestResult(repo_cfg.name, run_id, len(nodes), len(edges), "ok")
    except Exception as exc:  # per-repo isolation: never propagate, never touch prior rows
        print(f"[tpk] ingest failed for {repo_cfg.name}: {exc}")
        result = IngestResult(repo_cfg.name, run_id, 0, 0, "failed")
    _log(client, prefix, result, _git_sha(repo_cfg.path))
    return result
