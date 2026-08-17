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
from tpk.config import RepoConfig, Settings, config_path, entry_key, load_llm
from tpk.ingest import ingest_repo

REPOS_TOML = config_path()

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
    # Per-user daily token budget override (0 = inherit role/global, #62).
    daily_token_limit: int = 0


class UpdateUser(BaseModel):
    username: str
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None          # admin reset; sets must_change_password
    daily_token_limit: int | None = None  # omitted -> unchanged


class DeleteUser(BaseModel):
    username: str


class UpsertRole(BaseModel):
    name: str
    entry_keys: list[str]
    description: str = ""
    # Omitted -> the chat-only default (friendly for API-only callers); an
    # explicit [] is honored as a zero-capability role.
    capabilities: list[str] | None = None
    # Daily per-user token budget for this role's members (0 = unlimited, #62).
    daily_token_limit: int = 0


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
                "disabled": u.disabled,
                "daily_token_limit": u.daily_token_limit}

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

    # -- bounded delegation ------------------------------------------------
    # A non-admin actor with users:manage can delegate only within their own
    # grant: never the reserved admin role/users, and never a capability or
    # corpus entry their own role lacks. Admins are unbounded.

    def _actor_grant(client, actor):
        """(capabilities, entry_keys) the actor may hand out. Admin -> (None,
        None) meaning unbounded."""
        if actor.role == auth_mod.ROLE_ADMIN:
            return None, None
        role = auth_mod.get_role(client, actor.role, prefix=prefix)
        caps = auth_mod.effective_capabilities(actor, role)
        keys = set(role.entry_keys) if role else set()
        return caps, keys

    def _guard_admin_target(actor, target: "auth_mod.User"):
        if actor.role != auth_mod.ROLE_ADMIN and target.role == auth_mod.ROLE_ADMIN:
            raise HTTPException(403, "cannot manage admin users")

    def _guard_grant(client, actor, caps, entry_keys):
        """Reject a grant (role capabilities/entry_keys) exceeding the actor's
        own. No-op for admins."""
        own_caps, own_keys = _actor_grant(client, actor)
        if own_caps is None:
            return
        if not auth_mod.expand_capabilities(caps) <= own_caps:
            raise HTTPException(403, "cannot grant capabilities beyond your own")
        if not set(entry_keys) <= own_keys:
            raise HTTPException(403, "cannot grant corpus access beyond your own")

    def _guard_role_assignment(client, actor, role_name: str):
        """Guard assigning `role_name` to a user: non-admins may not assign
        admin, nor a role whose grant exceeds their own."""
        if actor.role == auth_mod.ROLE_ADMIN:
            return
        if role_name == auth_mod.ROLE_ADMIN:
            raise HTTPException(403, "cannot assign the admin role")
        role = auth_mod.get_role(client, role_name, prefix=prefix)
        if role is None:
            return  # _check_role_exists reports non-existence
        _guard_grant(client, actor, role.capabilities, role.entry_keys)

    @router.get("/repos")
    def list_repos(actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_VIEW))):
        client = _client()
        entries = corpus.list_entries(client, prefix=prefix)
        counts = dict(
            client.query(
                f"SELECT repo, count() FROM {db.latest(db.qualified('kg_nodes', prefix))} GROUP BY repo"
            ).result_rows
        )
        last: dict[str, tuple] = {}
        for repo, sha, status, t in client.query(
            f"SELECT repo, arg_max(git_sha, _tp_time), arg_max(status, _tp_time),"
            f" max(_tp_time) FROM table({db.qualified('kg_ingest_log', prefix)}) GROUP BY repo"
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
    def add_repo(body: AddRepo, actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_MANAGE))):
        cfg = _validate(body)
        corpus.upsert_entry(_client(), cfg, prefix=prefix)
        job_id = jobs.submit(cfg) if body.ingest else None
        return {"entry_key": entry_key(cfg), "job_id": job_id}

    @router.post("/repos/toggle")
    def toggle_repo(body: ToggleRepo, actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_MANAGE))):
        if not corpus.set_enabled(_client(), body.name, body.ref, body.enabled, prefix=prefix):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/delete")
    def delete_repo(body: DeleteRepo, actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_MANAGE))):
        if not corpus.delete_entry(_client(), body.name, body.ref, prefix=prefix,
                                   purge=body.purge):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/reindex")
    def reindex_repo(body: EntryRef, actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_MANAGE))):
        cfg = corpus.find_entry(_client(), body.name, body.ref, prefix=prefix)
        if cfg is None:
            raise HTTPException(404, "no such corpus entry")
        return {"job_id": jobs.submit(cfg)}

    @router.get("/jobs")
    def list_jobs(actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_VIEW))):
        return sorted(jobs.snapshot(), key=lambda j: j["submitted_at"], reverse=True)[:50]

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, actor: User = Depends(auth.require_cap(auth_mod.CAP_CORPUS_VIEW))):
        rec = jobs.get(job_id)
        if rec is None:
            raise HTTPException(404, "no such job")
        return rec

    @router.get("/users")
    def api_list_users(actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_VIEW))):
        return [_user_json(u) for u in auth_mod.list_users(_client(), prefix=prefix)]

    @router.post("/users")
    def api_add_user(body: AddUser, actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        if not _USERNAME_RE.match(body.username) or body.username in (".", ".."):
            raise HTTPException(400, "username must match ^[A-Za-z0-9._-]+$")
        err = auth_mod.validate_new_password(body.password)
        if err:
            raise HTTPException(400, err)
        client = _client()
        if auth_mod.get_user(client, body.username, prefix=prefix) is not None:
            raise HTTPException(409, "user already exists")
        if body.daily_token_limit < 0:
            raise HTTPException(400, "daily_token_limit must be >= 0 (0 = inherit)")
        _check_role_exists(client, body.role)
        _guard_role_assignment(client, actor, body.role)
        auth_mod.upsert_user(client, auth_mod.User(
            body.username, auth_mod.hash_password(body.password), body.role,
            must_change_password=body.must_change_password,
            daily_token_limit=body.daily_token_limit), prefix=prefix)
        return {"ok": True}

    @router.post("/users/update")
    def api_update_user(body: UpdateUser, actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        client = _client()
        u = auth_mod.get_user(client, body.username, prefix=prefix)
        if u is None:
            raise HTTPException(404, "no such user")
        _guard_admin_target(actor, u)
        _guard_last_admin(client, body.username, body.role, body.disabled)
        role = body.role if body.role is not None else u.role
        if body.role is not None:
            _check_role_exists(client, role)
            _guard_role_assignment(client, actor, role)
        disabled = body.disabled if body.disabled is not None else u.disabled
        if body.daily_token_limit is not None and body.daily_token_limit < 0:
            raise HTTPException(400, "daily_token_limit must be >= 0 (0 = inherit)")
        token_limit = body.daily_token_limit if body.daily_token_limit is not None \
            else u.daily_token_limit
        password_hash, must_change = u.password_hash, u.must_change_password
        if body.password is not None:
            err = auth_mod.validate_new_password(body.password)
            if err:
                raise HTTPException(400, err)
            password_hash, must_change = auth_mod.hash_password(body.password), True
        auth_mod.upsert_user(client, auth_mod.User(
            u.username, password_hash, role, must_change, disabled,
            daily_token_limit=token_limit), prefix=prefix)
        if disabled or body.password is not None:
            auth_mod.delete_user_sessions(client, u.username, prefix=prefix)
        return {"ok": True}

    @router.post("/users/delete")
    def api_delete_user(body: DeleteUser, actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        client = _client()
        u = auth_mod.get_user(client, body.username, prefix=prefix)
        if u is None:
            raise HTTPException(404, "no such user")
        _guard_admin_target(actor, u)
        if u.role == auth_mod.ROLE_ADMIN and not u.disabled \
                and auth_mod.admin_count(client, prefix=prefix) <= 1:
            raise HTTPException(400, "cannot demote/disable/delete the last admin")
        auth_mod.delete_user(client, body.username, prefix=prefix)
        auth_mod.delete_user_sessions(client, body.username, prefix=prefix)
        return {"ok": True}

    @router.get("/roles")
    def api_list_roles(actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_VIEW))):
        return [{"name": r.name, "entry_keys": r.entry_keys,
                 "capabilities": r.capabilities, "description": r.description,
                 "daily_token_limit": r.daily_token_limit}
                for r in auth_mod.list_roles(_client(), prefix=prefix)]

    @router.post("/roles")
    def api_upsert_role(body: UpsertRole, actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        if body.name == auth_mod.ROLE_ADMIN:
            raise HTTPException(400, "'admin' is a reserved role name")
        if not _USERNAME_RE.match(body.name):
            raise HTTPException(400, "role name must match ^[A-Za-z0-9._-]+$")
        if any(not isinstance(k, str) or not k for k in body.entry_keys):
            raise HTTPException(400, "entry_keys must be non-empty strings")
        caps = body.capabilities if body.capabilities is not None \
            else list(auth_mod.DEFAULT_CAPABILITIES)
        unknown = [c for c in caps if c not in auth_mod.ALL_CAPABILITIES]
        if unknown:
            raise HTTPException(400, f"unknown capabilities: {', '.join(unknown)}")
        if body.daily_token_limit < 0:
            raise HTTPException(400, "daily_token_limit must be >= 0 (0 = unlimited)")
        client = _client()
        # Guard both the new values AND (when overwriting) the role's current
        # privileges -- else a non-admin manager could neuter a role more
        # privileged than their own grant, stripping its members (mirrors the
        # delete-path guard).
        existing = auth_mod.get_role(client, body.name, prefix=prefix)
        if existing is not None:
            _guard_grant(client, actor, existing.capabilities, existing.entry_keys)
        _guard_grant(client, actor, caps, body.entry_keys)
        auth_mod.upsert_role(client, auth_mod.Role(
            body.name, body.entry_keys, body.description, caps,
            daily_token_limit=body.daily_token_limit), prefix=prefix)
        return {"ok": True}

    @router.post("/roles/delete")
    def api_delete_role(body: DeleteRole, actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        client = _client()
        role = auth_mod.get_role(client, body.name, prefix=prefix)
        if role is None:
            raise HTTPException(404, "no such role")
        # A non-admin manager may not delete a role more privileged than their
        # own grant.
        _guard_grant(client, actor, role.capabilities, role.entry_keys)
        if auth_mod.usernames_with_role(client, body.name, prefix=prefix):
            raise HTTPException(409, "role is assigned to users")
        auth_mod.delete_role(client, body.name, prefix=prefix)
        return {"ok": True}

    return router
