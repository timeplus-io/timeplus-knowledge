"""Management API: corpus CRUD + background ingest jobs."""

import queue
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import tpk.auth as auth_mod
from tpk import corpus, db
from tpk.auth import AuthLayer, User
from tpk.config import RepoConfig, Settings, entry_key, load_llm
from tpk.ingest import ingest_repo

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"

# `name` keys the graph (`entry_key`) and is also used as a raw filesystem
# path segment (`resolved_repo_path` in tpk.config: `checkout_root() / name /
# ref`). This regex alone rejects empty and `@`/`/`, but NOT the literal
# strings "." or ".." (both match `[A-Za-z0-9._-]+`) -- those are rejected
# separately below, since "..", used as a path segment, walks the checkout
# destination out of the checkout cache entirely.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# `ref` is passed straight to `git fetch`/`git clone` and used to build
# checkout/out-dir paths. `/` is allowed (branch names like "release/1.0"),
# but a leading `-` (git option injection), a leading `/`, `..` path
# components, or anything outside this character class (incl. whitespace)
# is rejected.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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


class AddUser(BaseModel):
    username: str
    password: str
    role: str
    must_change_password: bool = True


class UpdateUser(BaseModel):
    username: str
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None          # admin reset; sets must_change_password


class DeleteUser(BaseModel):
    username: str


class UpsertRole(BaseModel):
    name: str
    entry_keys: list[str]
    description: str = ""


class DeleteRole(BaseModel):
    name: str


def _validate(body: AddRepo) -> RepoConfig:
    if not _NAME_RE.match(body.name) or body.name in (".", ".."):
        raise HTTPException(400, "name must match ^[A-Za-z0-9._-]+$ and not be '.' or '..'")
    if body.ref:
        if body.ref.startswith("-"):
            raise HTTPException(400, "ref must not start with '-'")
        if body.ref.startswith("/"):
            raise HTTPException(400, "ref must not start with '/'")
        if ".." in body.ref.split("/"):
            raise HTTPException(400, "ref must not contain '..' path components")
        if not _REF_RE.match(body.ref):
            raise HTTPException(400, "ref must match ^[A-Za-z0-9._/-]+$")
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
        # Guards all access to `self.jobs` (insert in submit(), snapshot in
        # list/get, field updates in the worker thread). Distinct from
        # `_lock` (worker-start guard) so `submit()` -> `_ensure_worker()`
        # doesn't need a reentrant lock.
        self._jobs_lock = threading.Lock()

    def _ensure_worker(self):
        with self._lock:
            if not self._started:
                threading.Thread(target=self._run, daemon=True).start()
                self._started = True

    def submit(self, cfg: RepoConfig) -> str:
        job_id = uuid.uuid4().hex[:12]
        rec = {
            "id": job_id, "entry_key": entry_key(cfg), "status": "queued",
            "nodes": 0, "edges": 0, "error": None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        with self._jobs_lock:
            self.jobs[job_id] = rec
        self._q.put((job_id, cfg))
        self._ensure_worker()
        return job_id

    def snapshot(self) -> list[dict]:
        with self._jobs_lock:
            return list(self.jobs.values())

    def get(self, job_id: str) -> dict | None:
        with self._jobs_lock:
            return self.jobs.get(job_id)

    def _run(self):
        while True:
            job_id, cfg = self._q.get()
            with self._jobs_lock:
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
                with self._jobs_lock:
                    rec["nodes"], rec["edges"] = result.nodes, result.edges
                    rec["status"] = result.status  # "ok" | "failed"
                    if result.status == "failed":
                        rec["error"] = "ingest failed; see server logs"
            except Exception as exc:
                with self._jobs_lock:
                    rec["status"] = "failed"
                    rec["error"] = f"{type(exc).__name__}: {exc}"
            with self._jobs_lock:
                rec["finished_at"] = datetime.now(timezone.utc).isoformat()


def create_api_router(prefix: str = "", auth=None) -> APIRouter:
    auth = auth or AuthLayer(prefix)
    router = APIRouter(prefix="/api")
    jobs = JobManager(prefix=prefix)

    def _client():
        return db.get_client(Settings.from_env())

    def _user_json(u):
        return {"username": u.username, "role": u.role,
                "must_change_password": u.must_change_password,
                "disabled": u.disabled}

    def _check_role_exists(client, name: str):
        if name != auth_mod.ROLE_ADMIN and auth_mod.get_role(client, name, prefix=prefix) is None:
            raise HTTPException(400, f"role {name!r} does not exist")

    def _guard_last_admin(client, username: str, new_role: str | None, new_disabled: bool | None):
        u = auth_mod.get_user(client, username, prefix=prefix)
        if u is None or u.role != auth_mod.ROLE_ADMIN or u.disabled:
            return
        demoted = new_role is not None and new_role != auth_mod.ROLE_ADMIN
        disabled = new_disabled is True
        if (demoted or disabled) and auth_mod.admin_count(client, prefix=prefix) <= 1:
            raise HTTPException(400, "cannot demote/disable/delete the last admin")

    @router.get("/repos")
    def list_repos(admin: User = Depends(auth.require_admin)):
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
    def add_repo(body: AddRepo, admin: User = Depends(auth.require_admin)):
        cfg = _validate(body)
        corpus.upsert_entry(_client(), cfg, prefix=prefix)
        job_id = jobs.submit(cfg) if body.ingest else None
        return {"entry_key": entry_key(cfg), "job_id": job_id}

    @router.post("/repos/toggle")
    def toggle_repo(body: ToggleRepo, admin: User = Depends(auth.require_admin)):
        if not corpus.set_enabled(_client(), body.name, body.ref, body.enabled, prefix=prefix):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/delete")
    def delete_repo(body: DeleteRepo, admin: User = Depends(auth.require_admin)):
        if not corpus.delete_entry(_client(), body.name, body.ref, prefix=prefix,
                                   purge=body.purge):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/reindex")
    def reindex_repo(body: EntryRef, admin: User = Depends(auth.require_admin)):
        cfg = corpus.find_entry(_client(), body.name, body.ref, prefix=prefix)
        if cfg is None:
            raise HTTPException(404, "no such corpus entry")
        return {"job_id": jobs.submit(cfg)}

    @router.get("/jobs")
    def list_jobs(admin: User = Depends(auth.require_admin)):
        return sorted(jobs.snapshot(), key=lambda j: j["submitted_at"], reverse=True)[:50]

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, admin: User = Depends(auth.require_admin)):
        rec = jobs.get(job_id)
        if rec is None:
            raise HTTPException(404, "no such job")
        return rec

    @router.get("/users")
    def api_list_users(admin: User = Depends(auth.require_admin)):
        return [_user_json(u) for u in auth_mod.list_users(_client(), prefix=prefix)]

    @router.post("/users")
    def api_add_user(body: AddUser, admin: User = Depends(auth.require_admin)):
        if not _USERNAME_RE.match(body.username) or body.username in (".", ".."):
            raise HTTPException(400, "username must match ^[A-Za-z0-9._-]+$")
        err = auth_mod.validate_new_password(body.password)
        if err:
            raise HTTPException(400, err)
        client = _client()
        if auth_mod.get_user(client, body.username, prefix=prefix) is not None:
            raise HTTPException(409, "user already exists")
        _check_role_exists(client, body.role)
        auth_mod.upsert_user(client, auth_mod.User(
            body.username, auth_mod.hash_password(body.password), body.role,
            must_change_password=body.must_change_password), prefix=prefix)
        return {"ok": True}

    @router.post("/users/update")
    def api_update_user(body: UpdateUser, admin: User = Depends(auth.require_admin)):
        client = _client()
        u = auth_mod.get_user(client, body.username, prefix=prefix)
        if u is None:
            raise HTTPException(404, "no such user")
        _guard_last_admin(client, body.username, body.role, body.disabled)
        role = body.role if body.role is not None else u.role
        if body.role is not None:
            _check_role_exists(client, role)
        disabled = body.disabled if body.disabled is not None else u.disabled
        password_hash, must_change = u.password_hash, u.must_change_password
        if body.password is not None:
            err = auth_mod.validate_new_password(body.password)
            if err:
                raise HTTPException(400, err)
            password_hash, must_change = auth_mod.hash_password(body.password), True
        auth_mod.upsert_user(client, auth_mod.User(
            u.username, password_hash, role, must_change, disabled), prefix=prefix)
        if disabled or body.password is not None:
            auth_mod.delete_user_sessions(client, u.username, prefix=prefix)
        return {"ok": True}

    @router.post("/users/delete")
    def api_delete_user(body: DeleteUser, admin: User = Depends(auth.require_admin)):
        client = _client()
        u = auth_mod.get_user(client, body.username, prefix=prefix)
        if u is None:
            raise HTTPException(404, "no such user")
        if u.role == auth_mod.ROLE_ADMIN and not u.disabled \
                and auth_mod.admin_count(client, prefix=prefix) <= 1:
            raise HTTPException(400, "cannot demote/disable/delete the last admin")
        auth_mod.delete_user(client, body.username, prefix=prefix)
        auth_mod.delete_user_sessions(client, body.username, prefix=prefix)
        return {"ok": True}

    @router.get("/roles")
    def api_list_roles(admin: User = Depends(auth.require_admin)):
        return [{"name": r.name, "entry_keys": r.entry_keys, "description": r.description}
                for r in auth_mod.list_roles(_client(), prefix=prefix)]

    @router.post("/roles")
    def api_upsert_role(body: UpsertRole, admin: User = Depends(auth.require_admin)):
        if body.name == auth_mod.ROLE_ADMIN:
            raise HTTPException(400, "'admin' is a reserved role name")
        if not _USERNAME_RE.match(body.name):
            raise HTTPException(400, "role name must match ^[A-Za-z0-9._-]+$")
        if any(not isinstance(k, str) or not k for k in body.entry_keys):
            raise HTTPException(400, "entry_keys must be non-empty strings")
        auth_mod.upsert_role(_client(), auth_mod.Role(
            body.name, body.entry_keys, body.description), prefix=prefix)
        return {"ok": True}

    @router.post("/roles/delete")
    def api_delete_role(body: DeleteRole, admin: User = Depends(auth.require_admin)):
        client = _client()
        if auth_mod.get_role(client, body.name, prefix=prefix) is None:
            raise HTTPException(404, "no such role")
        if auth_mod.usernames_with_role(client, body.name, prefix=prefix):
            raise HTTPException(409, "role is assigned to users")
        auth_mod.delete_role(client, body.name, prefix=prefix)
        return {"ok": True}

    return router
