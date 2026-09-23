"""Per-repo ingest: graphify -> parse -> upsert -> stale-delete -> log."""

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tpk import db
from tpk.config import RepoConfig, entry_key
from tpk.fetch import fetch_github_repo
from tpk.graphify_runner import parse_graph_json, run_graphify
from tpk.model import EDGE_COLUMNS, NODE_COLUMNS, Edge, Node, edge_rows, node_rows


@dataclass(frozen=True)
class IngestResult:
    repo: str
    run_id: str
    nodes: int
    edges: int
    status: str  # "ok" | "failed"


@dataclass(frozen=True)
class IngestProgress:
    phase: str            # "fetch" | "extract" | "parse" | "upsert" | "done"
    message: str = ""
    nodes: int = 0
    edges: int = 0


ProgressFn = Callable[[IngestProgress], None]


def upsert_graph(
    client,
    prefix: str,
    nodes: list[Node],
    edges: list[Edge],
    run_started_at: datetime,
    repos: set[str] | None = None,
) -> None:
    if nodes:
        client.insert(db.qualified("kg_nodes", prefix), node_rows(nodes, run_started_at), column_names=NODE_COLUMNS)
    if edges:
        client.insert(db.qualified("kg_edges", prefix), edge_rows(edges, run_started_at), column_names=EDGE_COLUMNS)
    if repos is None:
        # Derived from the batch: callers seeding multi-repo test data rely on
        # this. Note this means a repo whose parse yielded zero nodes/edges
        # would derive to an empty set here and its stale rows would never be
        # deleted -- ingest_repo avoids that by always passing `repos`
        # explicitly.
        repos = {n.repo for n in nodes} | {e.repo for e in edges}
    for repo in repos:
        params = {"r": repo, "t": run_started_at}
        where = "repo = %(r)s AND updated_at < %(t)s"
        db.delete(client, db.qualified("kg_nodes", prefix), where, params, ("id",))
        db.delete(client, db.qualified("kg_edges", prefix), where, params, ("src", "dst", "rel"))


def _git_sha(repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"], capture_output=True, text=True
        )
    except FileNotFoundError:  # no git binary (e.g. minimal container)
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _log(client, prefix: str, result: IngestResult, git_sha: str, error: str = "") -> None:
    client.insert(
        db.qualified("kg_ingest_log", prefix),
        [[result.repo, result.run_id, result.nodes, result.edges, git_sha, result.status, error[:2000]]],
        column_names=["repo", "run_id", "nodes", "edges", "git_sha", "status", "error"],
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
    on_progress: ProgressFn | None = None,
) -> IngestResult:
    run_id = uuid.uuid4().hex[:12]
    run_started_at = datetime.now(timezone.utc)
    key = entry_key(repo_cfg)
    repo_path = repo_cfg.path

    def _report(phase: str, message: str = "", nodes: int = 0, edges: int = 0) -> None:
        if on_progress is not None:
            on_progress(IngestProgress(phase, message, nodes, edges))

    # A `started` row makes the run visible (UI ingest history, #15) while it
    # runs -- including CLI / kubectl runs the API's JobManager never sees. Its
    # ok/failed twin below shares the run_id. Best effort: a log failure must
    # not stop the ingest.
    try:
        _log(client, prefix, IngestResult(key, run_id, 0, 0, "started"), "")
    except Exception as exc:  # noqa: BLE001
        print(f"[tpk] could not log ingest start for {key}: {exc}")
    error = ""
    try:
        if repo_cfg.github:
            _report("fetch")
            repo_path = fetch_github_repo(repo_cfg)
        _report("extract")
        graph_json = run_graphify(
            repo_path,
            # `key` (name@ref) can contain '/' for branch/tag-style refs
            # (e.g. "helm@release/1.0") -- sanitize the same way
            # `resolved_repo_path` sanitizes the checkout dir, so a ref with
            # '/' doesn't create nested graphify-out directories.
            out_root / key.replace("/", "_"),
            extraction=repo_cfg.extraction,
            backend=backend,
            model=model,
            token_budget=token_budget,
            stream=stream,
            on_line=lambda line: _report("extract", message=line),
        )
        _report("parse")
        nodes, edges = parse_graph_json(graph_json, key, repo_cfg.visibility)
        _report("upsert", nodes=len(nodes), edges=len(edges))
        # `repos` is a set literal of {key, repo_cfg.name}: when a repo carries
        # a ref, this is two distinct values, and the bare-name member cleans
        # up any legacy rows written before entry keys existed.
        upsert_graph(client, prefix, nodes, edges, run_started_at, repos={key, repo_cfg.name})
        result = IngestResult(key, run_id, len(nodes), len(edges), "ok")
        _report("done", nodes=len(nodes), edges=len(edges))
    except Exception as exc:  # per-repo isolation: never propagate, never touch prior rows
        print(f"[tpk] ingest failed for {key}: {exc}")
        result = IngestResult(key, run_id, 0, 0, "failed")
        error = f"{type(exc).__name__}: {exc}"
    sha = _git_sha(repo_path) if repo_path else "unknown"
    if repo_cfg.ref:
        sha = f"{sha} ({repo_cfg.ref})"
    _log(client, prefix, result, sha, error)
    return result


# -- ingest history (#15) ---------------------------------------------------
# A `started` row with no ok/failed twin older than this is treated as
# abandoned: the process died mid-run (OOM-kill, pod restart) and will never
# write the end row.
STALE_AFTER = timedelta(hours=2)
_LOG_COLUMNS = ["repo", "run_id", "nodes", "edges", "git_sha", "status", "error", "_tp_time"]


def _iso(dt) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt is not None else None


def list_runs(client, prefix: str = "", limit: int = 50, entry: str | None = None,
              now: datetime | None = None) -> list[dict]:
    """Ingest runs, newest first, folding each run's `started` and ok/failed
    rows (same run_id) into one record. Rows written before #15 have no
    `started` twin: they come back with started_at / duration_s = None."""
    now = now or datetime.now(timezone.utc)
    clauses, params = [], {}
    if entry:
        clauses.append("repo = %(repo)s")
        params["repo"] = entry
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # Newest rows first; a run's two rows are seconds-to-minutes apart, so a
    # generous window (4x the page) is enough to see both for every run on the
    # page -- a truncated run at the very end just lacks its start time.
    rows = client.query(
        f"SELECT {', '.join(_LOG_COLUMNS)} FROM table({db.qualified('kg_ingest_log', prefix)})"
        f"{where} ORDER BY _tp_time DESC LIMIT %(n)s",
        parameters={**params, "n": max(limit * 4, 200)},
    ).result_rows
    runs: dict[str, dict] = {}
    order: list[str] = []
    for repo, run_id, nodes, edges, sha, status, error, at in rows:
        at = at.replace(tzinfo=timezone.utc)
        rec = runs.get(run_id)
        if rec is None:
            rec = runs[run_id] = {"run_id": run_id, "entry_key": repo, "status": None, "nodes": 0,
                                  "edges": 0, "git_sha": "", "error": "", "started_at": None,
                                  "finished_at": None, "duration_s": None, "_sort": at}
            order.append(run_id)
        if status == "started":
            rec["started_at"] = at
        else:
            rec.update(status=status, nodes=nodes, edges=edges, git_sha=sha, error=error or "",
                       finished_at=at)
    out = []
    for run_id in order[:limit]:
        rec = runs[run_id]
        if rec["status"] is None:                   # only a `started` row
            age = now - rec["started_at"]
            if age > STALE_AFTER:
                rec["status"] = "stale"
                mins = int(age.total_seconds() // 60)
                ago = f"{mins // 60}h {mins % 60}m" if mins >= 120 else f"{mins} min"
                rec["error"] = f"started {ago} ago and did not finish (no end row)"
            else:
                rec["status"] = "running"
        if rec["started_at"] and rec["finished_at"]:
            rec["duration_s"] = round((rec["finished_at"] - rec["started_at"]).total_seconds(), 1)
        rec["started_at"], rec["finished_at"] = _iso(rec["started_at"]), _iso(rec["finished_at"])
        rec.pop("_sort")
        out.append(rec)
    return out


def latest_status(client, prefix: str = "", now: datetime | None = None) -> dict[str, tuple]:
    """Per entry key: (git_sha, status, time) of the most recent row, with an
    abandoned `started` reported as "stale" (same rule as list_runs)."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, tuple] = {}
    for repo, sha, status, t in client.query(
        f"SELECT repo, arg_max(git_sha, _tp_time), arg_max(status, _tp_time),"
        f" max(_tp_time) FROM table({db.qualified('kg_ingest_log', prefix)}) GROUP BY repo"
    ).result_rows:
        if status == "started" and now - t.replace(tzinfo=timezone.utc) > STALE_AFTER:
            status = "stale"
        out[repo] = (sha, status, t)
    return out

