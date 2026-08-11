"""Management API: corpus CRUD + background ingest jobs."""

import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from tpk import corpus, db
from tpk.config import RepoConfig, Settings, entry_key, load_llm
from tpk.ingest import ingest_repo

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


def _require_admin(x_admin_token: str | None) -> None:
    expected = os.environ.get("TPK_ADMIN_TOKEN")
    if expected and x_admin_token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Token")


class AddRepo(BaseModel):
    name: str
    github: str = ""
    ref: str = ""
    path: str = ""
    visibility: str = "internal"
    extraction: str = "code-only"
    description: str = ""
    enabled: bool = True
    ingest: bool = True


class EntryRef(BaseModel):
    name: str
    ref: str = ""


class ToggleRepo(EntryRef):
    enabled: bool


class DeleteRepo(EntryRef):
    purge: bool = False


def _validate(body: AddRepo) -> RepoConfig:
    if "@" in body.name:
        raise HTTPException(422, "repo name must not contain '@'")
    if bool(body.github) == bool(body.path):
        raise HTTPException(422, "set exactly one of 'github' or 'path'")
    if body.github and not body.ref:
        raise HTTPException(422, "'github' requires a 'ref' (tag/branch)")
    if body.visibility not in ("internal", "public"):
        raise HTTPException(422, "visibility must be internal|public")
    if body.extraction not in ("code-only", "semantic"):
        raise HTTPException(422, "extraction must be code-only|semantic")
    return RepoConfig(
        name=body.name, github=body.github, ref=body.ref,
        path=Path(body.path) if body.path else None,
        visibility=body.visibility, extraction=body.extraction,
        description=body.description, enabled=body.enabled,
    )


class JobManager:
    """One worker thread; jobs use their own DB client (never the KG's)."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.jobs: dict[str, dict] = {}
        self._q: queue.Queue = queue.Queue()
        self._started = False
        self._lock = threading.Lock()

    def _ensure_worker(self):
        with self._lock:
            if not self._started:
                threading.Thread(target=self._run, daemon=True).start()
                self._started = True

    def submit(self, cfg: RepoConfig) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = {
            "id": job_id, "entry_key": entry_key(cfg), "status": "queued",
            "nodes": 0, "edges": 0, "error": None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        self._q.put((job_id, cfg))
        self._ensure_worker()
        return job_id

    def _run(self):
        while True:
            job_id, cfg = self._q.get()
            rec = self.jobs[job_id]
            rec["status"] = "running"
            try:
                client = db.get_client(Settings.from_env())
                llm = load_llm(REPOS_TOML) if REPOS_TOML.exists() else None
                backend = None if (llm is None or llm.backend == "auto") else llm.backend
                result = ingest_repo(
                    client, cfg, prefix=self.prefix, backend=backend,
                    model=(llm.model or None) if llm else None,
                    token_budget=llm.token_budget if llm else 0,
                )
                rec["nodes"], rec["edges"] = result.nodes, result.edges
                rec["status"] = result.status  # "ok" | "failed"
                if result.status == "failed":
                    rec["error"] = "ingest failed; see server logs"
            except Exception as exc:
                rec["status"] = "failed"
                rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["finished_at"] = datetime.now(timezone.utc).isoformat()


def create_api_router(prefix: str = "") -> APIRouter:
    router = APIRouter(prefix="/api")
    jobs = JobManager(prefix=prefix)

    def _client():
        return db.get_client(Settings.from_env())

    @router.get("/repos")
    def list_repos(x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        client = _client()
        entries = corpus.list_entries(client, prefix=prefix)
        counts = dict(
            client.query(
                f"SELECT repo, count() FROM table({prefix}kg_nodes) GROUP BY repo"
            ).result_rows
        )
        last: dict[str, tuple] = {}
        for repo, sha, status, t in client.query(
            f"SELECT repo, arg_max(git_sha, _tp_time), arg_max(status, _tp_time),"
            f" max(_tp_time) FROM table({prefix}kg_ingest_log) GROUP BY repo"
        ).result_rows:
            last[repo] = (sha, status, t)
        out = []
        for e in entries:
            key = entry_key(e)
            sha, status, t = last.get(key, (None, None, None))
            out.append({
                "name": e.name, "ref": e.ref, "entry_key": key,
                "github": e.github, "path": str(e.path) if e.path else "",
                "enabled": e.enabled, "visibility": e.visibility,
                "extraction": e.extraction, "description": e.description,
                "node_count": counts.get(key, 0),
                "last_status": status, "last_sha": sha,
                "last_time": str(t) if t else None,
            })
        return out

    @router.post("/repos")
    def add_repo(body: AddRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        cfg = _validate(body)
        corpus.upsert_entry(_client(), cfg, prefix=prefix)
        job_id = jobs.submit(cfg) if body.ingest else None
        return {"entry_key": entry_key(cfg), "job_id": job_id}

    @router.post("/repos/toggle")
    def toggle_repo(body: ToggleRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if not corpus.set_enabled(_client(), body.name, body.ref, body.enabled, prefix=prefix):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/delete")
    def delete_repo(body: DeleteRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if not corpus.delete_entry(_client(), body.name, body.ref, prefix=prefix,
                                   purge=body.purge):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/reindex")
    def reindex_repo(body: EntryRef, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        cfg = corpus.find_entry(_client(), body.name, body.ref, prefix=prefix)
        if cfg is None:
            raise HTTPException(404, "no such corpus entry")
        return {"job_id": jobs.submit(cfg)}

    @router.get("/jobs")
    def list_jobs(x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        return sorted(jobs.jobs.values(), key=lambda j: j["submitted_at"], reverse=True)[:50]

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if job_id not in jobs.jobs:
            raise HTTPException(404, "no such job")
        return jobs.jobs[job_id]

    return router
