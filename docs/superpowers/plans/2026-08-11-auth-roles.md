# Authentication & Role-Based Repo Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Login-based auth for the knowledge stack: users + roles stored in Timeplus, roles scope which corpus entries chat can query, seeded `admin`/`changeme` with forced password change, and a dedicated timeplusd DB user in compose. Implements issue #8 per the approved spec `docs/superpowers/specs/2026-08-11-auth-roles-design.md`.

**Architecture:** New `src/tpk/auth.py` holds the store (three mutable streams: `kg_users`, `kg_roles`, `kg_sessions`, mirroring `corpus.py`), argon2id hashing, an `AuthLayer` of FastAPI dependencies, and an `/auth` router. The management API swaps its `X-Admin-Token` gate for `require_admin` and gains `/api/users` + `/api/roles` CRUD. Chat scoping rides the existing corpus filter in `KnowledgeGraph`: a request-scoped `ContextVar` of allowed entry keys is intersected with the enabled-entries set. The web UI gains a login gate, forced-change screen, and an admin Users tab. The compose stack provisions a `tpk` DB user via an entrypoint wrapper.

**Tech Stack:** Python 3.11, FastAPI, timeplus_connect, argon2-cffi (new dep), React + Vite, docker compose.

## Global Constraints

- Password hashing: argon2id via `argon2-cffi`'s `PasswordHasher` defaults. Plaintext credentials are never stored or logged.
- Session tokens: `secrets.token_urlsafe(32)`; only the SHA-256 hex is stored (`kg_sessions` key). Raw tokens are never stored or logged.
- `admin` is a **reserved role name, never a `kg_roles` row**; `user.role == "admin"` short-circuits to full access; role CRUD rejects the name.
- Seeded account: username `admin`, password `changeme`, `must_change_password=true`; seeded only when `kg_users` is empty; never re-seeded once any user exists.
- Password policy: ≥ 8 chars, ≠ `changeme`, ≠ old password.
- Login failures (unknown user, wrong password, disabled) all return the identical 401 body `{"detail": "invalid credentials"}` — no user enumeration.
- While `must_change_password` is true, every authenticated endpoint except `/auth/change-password`, `/auth/me`, `/auth/logout` returns 403 `{"detail": {"code": "password_change_required"}}`.
- Auth dependencies fail closed: unreadable user store → 503, never a grant.
- `TPK_ADMIN_TOKEN` / `X-Admin-Token` are removed everywhere (code, compose, `.env.example`, README, UI), including the path-entries-require-token rule (path entries now just require admin).
- The role-scope ContextVar is set per request and reset in `finally`.
- Session TTL: env `TPK_SESSION_TTL` seconds, default `86400`.
- `TPK_STREAM_PREFIX` prefixes the new streams exactly like existing ones; tests use per-test prefixes and `_eventually` polling (mutable streams have ~100-300ms write→read lag).
- Conventional commits; full suite green (`TIMEPLUS_HOST=localhost uv run pytest -q`) before each commit; `npm run build` and `npm run check:sanitize` green for UI changes.
- Finishing: push the feature branch and open a PR (`gh pr create`, body `Closes #8` and `Closes #6` — Task 7 makes serve bootstrap eager, which is #6's ask) — do NOT merge; the user reviews the PR.

## File Structure

```
src/tpk/db.py           # + kg_users/kg_roles/kg_sessions in ensure/drop
src/tpk/auth.py         # NEW: store, hashing, AuthLayer deps, /auth router
src/tpk/api.py          # admin gate via AuthLayer; + /api/users, /api/roles
src/tpk/server.py       # auth wiring, /chat require_user + role ContextVar
src/tpk/tools.py        # role-scope ContextVar intersected in corpus filter
src/tpk/cli.py          # serve: eager ensure_schema + seeds
web/src/api.ts          # NEW: fetch helper with bearer + 401 handling
web/src/Login.tsx       # NEW: login + forced password change
web/src/Users.tsx       # NEW: admin users + roles tab
web/src/App.tsx         # auth gate, header, tabs
web/src/Manage.tsx      # bearer headers; token box removed
deploy/docker/tpk-entrypoint.sh  # NEW: renders users.d from env
deploy/docker/Dockerfile         # entrypoint wrapper
docker-compose.yml, .env.example, README.md
tests/test_auth.py, tests/test_auth_api.py  # NEW
tests/test_api.py, tests/test_server.py, tests/test_tools.py, tests/test_db.py  # updated
```

---

### Task 1: Auth store — schema, hashing, users/roles/sessions CRUD, seeding

**Files:**
- Modify: `src/tpk/db.py` (extend `ensure_schema`, `drop_schema`)
- Create: `src/tpk/auth.py` (store half only — no FastAPI yet)
- Modify: `pyproject.toml` (via `uv add argon2-cffi`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `db.get_client`, `db.ensure_schema` patterns; `corpus.py` row-mapping style.
- Produces (Tasks 2-4 rely on these exact names):
  - `auth.User` dataclass: `username: str, password_hash: str, role: str, must_change_password: bool = False, disabled: bool = False`
  - `auth.Role` dataclass: `name: str, entry_keys: list[str], description: str = ""`
  - `auth.ROLE_ADMIN = "admin"`, `auth.SEED_USERNAME = "admin"`, `auth.SEED_PASSWORD = "changeme"`
  - `hash_password(password) -> str`, `verify_password(password_hash, password) -> bool`
  - `validate_new_password(new, old=None) -> str | None` (error message or None)
  - `upsert_user(client, user, prefix="")`, `get_user(client, username, prefix="") -> User | None`, `list_users(client, prefix="") -> list[User]`, `delete_user(client, username, prefix="")`
  - `upsert_role(client, role, prefix="")`, `get_role(client, name, prefix="") -> Role | None`, `list_roles(client, prefix="") -> list[Role]`, `delete_role(client, name, prefix="")`, `usernames_with_role(client, name, prefix="") -> list[str]`
  - `create_session(client, username, ttl_seconds, prefix="") -> str` (raw token), `get_session(client, token, prefix="") -> str | None` (username; lazily deletes expired), `delete_session(client, token, prefix="")`, `delete_user_sessions(client, username, prefix="", keep_token: str | None = None)`
  - `seed_admin(client, prefix="") -> bool` (True if seeded)
  - `admin_count(client, prefix="") -> int` (enabled admins: `role=="admin" and not disabled`)

- [ ] **Step 1: Add the dependency**

```bash
uv add argon2-cffi
```

- [ ] **Step 2: Extend the schema**

In `src/tpk/db.py` `ensure_schema`, after the `kg_repos` block add:

```python
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
```

In `drop_schema`, extend the tuple: `("kg_nodes", "kg_edges", "kg_ingest_log", "kg_repos", "kg_users", "kg_roles", "kg_sessions")`.

- [ ] **Step 3: Write failing store tests**

`tests/test_auth.py` (follow `tests/test_corpus.py`'s fixture pattern for prefix + client + `_eventually`; copy its conftest usage):

```python
import pytest

from tpk import auth, db

pytestmark = pytest.mark.usefixtures("timeplus")  # match test_corpus.py's skip-without-TIMEPLUS_HOST marker/fixture names


def test_password_hash_roundtrip():
    h = auth.hash_password("s3cret-pw")
    assert h != "s3cret-pw" and h.startswith("$argon2id$")
    assert auth.verify_password(h, "s3cret-pw")
    assert not auth.verify_password(h, "wrong")
    assert not auth.verify_password("not-a-hash", "s3cret-pw")


def test_validate_new_password():
    assert auth.validate_new_password("short") is not None
    assert auth.validate_new_password("changeme") is not None
    assert auth.validate_new_password("samesame1", old="samesame1") is not None
    assert auth.validate_new_password("good-enough-1") is None


def test_user_crud(client, prefix):
    u = auth.User("alice", auth.hash_password("password-1"), role="support")
    auth.upsert_user(client, u, prefix=prefix)
    _eventually(lambda: auth.get_user(client, "alice", prefix=prefix) is not None)
    got = auth.get_user(client, "alice", prefix=prefix)
    assert got.role == "support" and not got.disabled and not got.must_change_password
    auth.delete_user(client, "alice", prefix=prefix)
    _eventually(lambda: auth.get_user(client, "alice", prefix=prefix) is None)


def test_role_crud_and_usernames_with_role(client, prefix):
    auth.upsert_role(client, auth.Role("support", ["docs@main", "helm-charts@v13"]), prefix=prefix)
    _eventually(lambda: auth.get_role(client, "support", prefix=prefix) is not None)
    assert auth.get_role(client, "support", prefix=prefix).entry_keys == ["docs@main", "helm-charts@v13"]
    auth.upsert_user(client, auth.User("bob", "x", role="support"), prefix=prefix)
    _eventually(lambda: auth.usernames_with_role(client, "support", prefix=prefix) == ["bob"])


def test_sessions_create_get_expire_delete(client, prefix):
    auth.upsert_user(client, auth.User("carol", "x", role="support"), prefix=prefix)
    token = auth.create_session(client, "carol", ttl_seconds=3600, prefix=prefix)
    _eventually(lambda: auth.get_session(client, token, prefix=prefix) == "carol")
    assert auth.get_session(client, "no-such-token", prefix=prefix) is None
    expired = auth.create_session(client, "carol", ttl_seconds=-1, prefix=prefix)
    _eventually(lambda: auth.get_session(client, expired, prefix=prefix) is None)  # lazy delete
    auth.delete_session(client, token, prefix=prefix)
    _eventually(lambda: auth.get_session(client, token, prefix=prefix) is None)


def test_delete_user_sessions_keep_current(client, prefix):
    t1 = auth.create_session(client, "dave", 3600, prefix=prefix)
    t2 = auth.create_session(client, "dave", 3600, prefix=prefix)
    auth.delete_user_sessions(client, "dave", prefix=prefix, keep_token=t1)
    _eventually(lambda: auth.get_session(client, t2, prefix=prefix) is None)
    assert auth.get_session(client, t1, prefix=prefix) == "dave"


def test_seed_admin_idempotent(client, prefix):
    assert auth.seed_admin(client, prefix=prefix) is True
    _eventually(lambda: auth.get_user(client, "admin", prefix=prefix) is not None)
    admin = auth.get_user(client, "admin", prefix=prefix)
    assert admin.role == auth.ROLE_ADMIN and admin.must_change_password
    assert auth.verify_password(admin.password_hash, auth.SEED_PASSWORD)
    assert auth.seed_admin(client, prefix=prefix) is False  # never re-seed
    assert auth.admin_count(client, prefix=prefix) == 1
```

Adapt fixture names to what `tests/test_corpus.py` actually defines (client/prefix creation + schema setup/teardown via `db.ensure_schema`/`db.drop_schema`); reuse its `_eventually` helper import.

- [ ] **Step 4: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_auth.py`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError` on `tpk.auth`.

- [ ] **Step 5: Implement the store**

`src/tpk/auth.py`:

```python
"""Users, roles, and sessions: the auth store (issue #8).

`admin` is a reserved role name, never a kg_roles row: `user.role ==
ROLE_ADMIN` short-circuits to full access everywhere.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

ROLE_ADMIN = "admin"
SEED_USERNAME = "admin"
SEED_PASSWORD = "changeme"

_hasher = PasswordHasher()  # argon2id defaults

_USER_COLUMNS = ["username", "password_hash", "role", "must_change_password",
                 "disabled", "created_at", "updated_at"]
_ROLE_COLUMNS = ["name", "entry_keys", "description", "updated_at"]
_SESSION_COLUMNS = ["token_hash", "username", "expires_at", "created_at"]


@dataclass
class User:
    username: str
    password_hash: str
    role: str
    must_change_password: bool = False
    disabled: bool = False


@dataclass
class Role:
    name: str
    entry_keys: list[str] = field(default_factory=list)
    description: str = ""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, ValueError):
        # VerificationError covers mismatch; ValueError covers a value that
        # is not an argon2 hash at all (e.g. empty test fixtures).
        return False


def validate_new_password(new: str, old: str | None = None) -> str | None:
    if len(new) < 8:
        return "password must be at least 8 characters"
    if new == SEED_PASSWORD:
        return "password must not be the seeded default"
    if old is not None and new == old:
        return "new password must differ from the old one"
    return None


def _now():
    return datetime.now(timezone.utc)


# -- users -----------------------------------------------------------------

def upsert_user(client, user: User, prefix: str = "") -> None:
    client.insert(
        f"{prefix}kg_users",
        [[user.username, user.password_hash, user.role,
          user.must_change_password, user.disabled, _now(), _now()]],
        column_names=_USER_COLUMNS,
    )


def get_user(client, username: str, prefix: str = "") -> User | None:
    rows = client.query(
        f"SELECT username, password_hash, role, must_change_password, disabled"
        f" FROM table({prefix}kg_users) WHERE username = %(u)s",
        parameters={"u": username},
    ).result_rows
    if not rows:
        return None
    u, h, r, mc, dis = rows[0]
    return User(u, h, r, bool(mc), bool(dis))


def list_users(client, prefix: str = "") -> list[User]:
    rows = client.query(
        f"SELECT username, password_hash, role, must_change_password, disabled"
        f" FROM table({prefix}kg_users) ORDER BY username"
    ).result_rows
    return [User(u, h, r, bool(mc), bool(dis)) for u, h, r, mc, dis in rows]


def delete_user(client, username: str, prefix: str = "") -> None:
    client.command(
        f"DELETE FROM {prefix}kg_users WHERE username = %(u)s",
        parameters={"u": username},
    )


def admin_count(client, prefix: str = "") -> int:
    rows = client.query(
        f"SELECT count() FROM table({prefix}kg_users)"
        f" WHERE role = %(r)s AND NOT disabled",
        parameters={"r": ROLE_ADMIN},
    ).result_rows
    return int(rows[0][0]) if rows else 0


def seed_admin(client, prefix: str = "") -> bool:
    if list_users(client, prefix=prefix):
        return False
    upsert_user(
        client,
        User(SEED_USERNAME, hash_password(SEED_PASSWORD), ROLE_ADMIN,
             must_change_password=True),
        prefix=prefix,
    )
    return True


# -- roles -----------------------------------------------------------------

def upsert_role(client, role: Role, prefix: str = "") -> None:
    client.insert(
        f"{prefix}kg_roles",
        [[role.name, json.dumps(role.entry_keys), role.description, _now()]],
        column_names=_ROLE_COLUMNS,
    )


def get_role(client, name: str, prefix: str = "") -> Role | None:
    rows = client.query(
        f"SELECT name, entry_keys, description FROM table({prefix}kg_roles)"
        f" WHERE name = %(n)s",
        parameters={"n": name},
    ).result_rows
    if not rows:
        return None
    n, keys, desc = rows[0]
    return Role(n, json.loads(keys) if keys else [], desc)


def list_roles(client, prefix: str = "") -> list[Role]:
    rows = client.query(
        f"SELECT name, entry_keys, description FROM table({prefix}kg_roles)"
        f" ORDER BY name"
    ).result_rows
    return [Role(n, json.loads(k) if k else [], d) for n, k, d in rows]


def delete_role(client, name: str, prefix: str = "") -> None:
    client.command(
        f"DELETE FROM {prefix}kg_roles WHERE name = %(n)s",
        parameters={"n": name},
    )


def usernames_with_role(client, name: str, prefix: str = "") -> list[str]:
    rows = client.query(
        f"SELECT username FROM table({prefix}kg_users) WHERE role = %(r)s"
        f" ORDER BY username",
        parameters={"r": name},
    ).result_rows
    return [r[0] for r in rows]


# -- sessions --------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(client, username: str, ttl_seconds: int, prefix: str = "") -> str:
    token = secrets.token_urlsafe(32)
    client.insert(
        f"{prefix}kg_sessions",
        [[_token_hash(token), username,
          _now() + timedelta(seconds=ttl_seconds), _now()]],
        column_names=_SESSION_COLUMNS,
    )
    return token


def get_session(client, token: str, prefix: str = "") -> str | None:
    h = _token_hash(token)
    rows = client.query(
        f"SELECT username, expires_at FROM table({prefix}kg_sessions)"
        f" WHERE token_hash = %(h)s",
        parameters={"h": h},
    ).result_rows
    if not rows:
        return None
    username, expires_at = rows[0]
    if expires_at.replace(tzinfo=timezone.utc) < _now():
        client.command(
            f"DELETE FROM {prefix}kg_sessions WHERE token_hash = %(h)s",
            parameters={"h": h},
        )
        return None
    return username


def delete_session(client, token: str, prefix: str = "") -> None:
    client.command(
        f"DELETE FROM {prefix}kg_sessions WHERE token_hash = %(h)s",
        parameters={"h": _token_hash(token)},
    )


def delete_user_sessions(client, username: str, prefix: str = "",
                         keep_token: str | None = None) -> None:
    sql = f"DELETE FROM {prefix}kg_sessions WHERE username = %(u)s"
    params = {"u": username}
    if keep_token is not None:
        sql += " AND token_hash != %(k)s"
        params["k"] = _token_hash(keep_token)
    client.command(sql, parameters=params)
```

Note: check how `test_corpus.py`/`corpus.py` compare `datetime64` values coming back from timeplus_connect — if `expires_at` arrives timezone-aware already, drop the `.replace(tzinfo=...)`; run the expiry test to confirm.

- [ ] **Step 6: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_auth.py tests/test_db.py`
Expected: PASS (also update `tests/test_db.py` if it asserts the exact stream list dropped/created — add the three new names).

- [ ] **Step 7: Run the full suite, then commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add pyproject.toml uv.lock src/tpk/db.py src/tpk/auth.py tests/test_auth.py tests/test_db.py
git commit -m "feat: auth store - kg_users/kg_roles/kg_sessions with argon2id hashing and admin seed"
```

---

### Task 2: Auth HTTP layer — AuthLayer dependencies + /auth router + server wiring

**Files:**
- Modify: `src/tpk/auth.py` (append the HTTP half)
- Modify: `src/tpk/server.py` (auth param on `create_app`, mount router, `/chat` requires a user)
- Modify: `src/tpk/cli.py` (eager bootstrap in `serve`)
- Test: `tests/test_auth_api.py` (new), `tests/test_server.py` (stub auth)

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces (Tasks 3-5 rely on these exact names):
  - `auth.AuthLayer(prefix="")` with dependency methods `require_user`, `require_user_any`, `require_admin` (each: `authorization: str | None = Header(None)` → `User`; raise `HTTPException`), and `_client()` returning a fresh timeplus client.
  - `auth.create_auth_router(auth_layer) -> APIRouter` mounting `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/change-password`.
  - `server.create_app(agent=None, stream_prefix="", auth=None)` — `auth=None` builds `AuthLayer(stream_prefix)`; tests inject a stub.
  - Login response JSON: `{"token": str, "role": str, "must_change_password": bool}`; `/auth/me` JSON: `{"username": str, "role": str, "must_change_password": bool}`.

- [ ] **Step 1: Write failing API tests**

`tests/test_auth_api.py` (real Timeplus + per-test prefix, patterned on `tests/test_api.py`'s app fixture — `create_app(agent=_NoAgent(), stream_prefix=prefix)`):

```python
def _login(c, username, password):
    return c.post("/auth/login", json={"username": username, "password": password})

def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_logout_me(c, client, prefix):
    auth.upsert_user(client, auth.User("alice", auth.hash_password("password-1"), "support"), prefix=prefix)
    _eventually(lambda: _login(c, "alice", "password-1").status_code == 200)
    body = _login(c, "alice", "password-1").json()
    assert body["role"] == "support" and body["must_change_password"] is False
    me = c.get("/auth/me", headers=_hdr(body["token"]))
    assert me.status_code == 200 and me.json()["username"] == "alice"
    assert c.post("/auth/logout", headers=_hdr(body["token"])).status_code == 204
    assert c.get("/auth/me", headers=_hdr(body["token"])).status_code == 401


def test_login_uniform_401(c, client, prefix):
    auth.upsert_user(client, auth.User("bob", auth.hash_password("password-1"), "support", disabled=True), prefix=prefix)
    r1 = _login(c, "nobody", "x")
    _eventually(lambda: _login(c, "bob", "password-1").status_code == 401)
    r2 = _login(c, "bob", "wrongpass")
    r3 = _login(c, "bob", "password-1")  # disabled
    assert r1.status_code == r2.status_code == r3.status_code == 401
    assert r1.json() == r2.json() == r3.json() == {"detail": "invalid credentials"}


def test_bad_bearer_401(c):
    assert c.get("/auth/me").status_code == 401
    assert c.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_must_change_password_gate_and_flow(c, client, prefix):
    assert auth.seed_admin(client, prefix=prefix)
    _eventually(lambda: _login(c, "admin", "changeme").status_code == 200)
    body = _login(c, "admin", "changeme").json()
    assert body["must_change_password"] is True
    t = body["token"]
    # gated endpoint blocked with the distinct code
    blocked = c.get("/api/repos", headers=_hdr(t))
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "password_change_required"
    # policy failures
    assert c.post("/auth/change-password", headers=_hdr(t),
                  json={"old_password": "changeme", "new_password": "short"}).status_code == 400
    assert c.post("/auth/change-password", headers=_hdr(t),
                  json={"old_password": "wrong", "new_password": "long-enough-1"}).status_code == 401
    # success clears the flag; same session keeps working
    ok = c.post("/auth/change-password", headers=_hdr(t),
                json={"old_password": "changeme", "new_password": "long-enough-1"})
    assert ok.status_code == 200
    _eventually(lambda: c.get("/auth/me", headers=_hdr(t)).json()["must_change_password"] is False)


def test_change_password_invalidates_other_sessions(c, client, prefix):
    auth.upsert_user(client, auth.User("carol", auth.hash_password("password-1"), "support"), prefix=prefix)
    _eventually(lambda: _login(c, "carol", "password-1").status_code == 200)
    t1 = _login(c, "carol", "password-1").json()["token"]
    t2 = _login(c, "carol", "password-1").json()["token"]
    assert c.post("/auth/change-password", headers=_hdr(t1),
                  json={"old_password": "password-1", "new_password": "password-2xy"}).status_code == 200
    _eventually(lambda: c.get("/auth/me", headers=_hdr(t2)).status_code == 401)
    assert c.get("/auth/me", headers=_hdr(t1)).status_code == 200


def test_chat_requires_user(c):
    assert c.post("/chat", json={"message": "hi"}).status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_auth_api.py`
Expected: FAIL (`create_auth_router` missing / 404s).

- [ ] **Step 3: Implement AuthLayer + router**

Append to `src/tpk/auth.py`:

```python
# -- HTTP layer ------------------------------------------------------------

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel


def _session_ttl() -> int:
    return int(os.environ.get("TPK_SESSION_TTL", "86400"))


class AuthLayer:
    """FastAPI dependencies over the auth store. One fresh client per
    request (matches api.py's `_client()` style); fails closed (503) when
    the store is unreachable."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def _client(self):
        from tpk import db
        from tpk.config import Settings

        return db.get_client(Settings.from_env())

    def _resolve(self, authorization: str | None) -> User:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            client = self._client()
            username = get_session(client, token, prefix=self.prefix)
            user = get_user(client, username, prefix=self.prefix) if username else None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "auth store unavailable")
        if user is None or user.disabled:
            raise HTTPException(401, "invalid or expired session")
        return user

    def require_user_any(self, authorization: str | None = Header(None)) -> User:
        """Valid session only — no must-change gate (me/logout/change-password)."""
        return self._resolve(authorization)

    def require_user(self, authorization: str | None = Header(None)) -> User:
        user = self._resolve(authorization)
        if user.must_change_password:
            raise HTTPException(403, detail={"code": "password_change_required"})
        return user

    def require_admin(self, authorization: str | None = Header(None)) -> User:
        user = self.require_user(authorization)
        if user.role != ROLE_ADMIN:
            raise HTTPException(403, "admin required")
        return user


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


def create_auth_router(auth_layer: AuthLayer) -> APIRouter:
    router = APIRouter(prefix="/auth")
    prefix = auth_layer.prefix

    @router.post("/login")
    def login(body: LoginRequest):
        try:
            client = auth_layer._client()
            user = get_user(client, body.username, prefix=prefix)
        except Exception:
            raise HTTPException(503, "auth store unavailable")
        if user is None or user.disabled or not verify_password(user.password_hash, body.password):
            raise HTTPException(401, "invalid credentials")
        token = create_session(client, user.username, _session_ttl(), prefix=prefix)
        return {"token": token, "role": user.role,
                "must_change_password": user.must_change_password}

    @router.post("/logout", status_code=204)
    def logout(authorization: str | None = Header(None),
               user: User = Depends(auth_layer.require_user_any)):
        delete_session(auth_layer._client(),
                       authorization.removeprefix("Bearer "), prefix=prefix)

    @router.get("/me")
    def me(user: User = Depends(auth_layer.require_user_any)):
        return {"username": user.username, "role": user.role,
                "must_change_password": user.must_change_password}

    @router.post("/change-password")
    def change_password(body: ChangePasswordRequest,
                        authorization: str | None = Header(None),
                        user: User = Depends(auth_layer.require_user_any)):
        if not verify_password(user.password_hash, body.old_password):
            raise HTTPException(401, "invalid credentials")
        err = validate_new_password(body.new_password, old=body.old_password)
        if err:
            raise HTTPException(400, err)
        client = auth_layer._client()
        upsert_user(client, User(user.username, hash_password(body.new_password),
                                 user.role, must_change_password=False,
                                 disabled=user.disabled), prefix=prefix)
        delete_user_sessions(client, user.username, prefix=prefix,
                             keep_token=authorization.removeprefix("Bearer "))
        return {"ok": True}

    return router
```

- [ ] **Step 4: Wire into the server and CLI**

`src/tpk/server.py` — change `create_app`'s signature and body:

```python
def create_app(agent=None, stream_prefix: str = "", auth=None) -> FastAPI:
    from tpk.agent import RECURSION_LIMIT
    from tpk.api import create_api_router
    from tpk.auth import AuthLayer, User, create_auth_router

    auth = auth or AuthLayer(stream_prefix)
    app = FastAPI(title="timeplus-knowledge")
    app.include_router(create_auth_router(auth))
    app.include_router(create_api_router(prefix=stream_prefix, auth=auth))
```

and add the dependency to `/chat` (`from fastapi import Depends`):

```python
    @app.post("/chat")
    async def chat(req: ChatRequest, user: User = Depends(auth.require_user)):
```

(`create_api_router(prefix=..., auth=...)` lands in Task 3 — to keep this task green, add the `auth` parameter to `create_api_router`'s signature now with a default of `None` and no behavior change; Task 3 makes it mandatory.)

`src/tpk/cli.py` `serve` — bootstrap eagerly before uvicorn (this is issue #6's fix):

```python
    from tpk import auth as auth_mod
    from tpk import corpus, db
    from tpk.api import REPOS_TOML
    from tpk.config import Settings
    from tpk.server import create_app

    client = db.get_client(Settings.from_env())
    db.ensure_schema(client)
    if REPOS_TOML.exists():
        corpus.seed_from_toml(client, REPOS_TOML)
    auth_mod.seed_admin(client)
    uvicorn.run(create_app(), host=host, port=port)
```

- [ ] **Step 5: Update test_server.py with a stub auth**

At the top of `tests/test_server.py`:

```python
from tpk.auth import AuthLayer, User


class _StubAuth(AuthLayer):
    """Grants a fixed user without touching any store."""

    def __init__(self, user: User | None = None):
        super().__init__()
        self.user = user or User("tester", "", "admin")

    def _resolve(self, authorization):
        return self.user
```

Change every `create_app(agent=...)` call to `create_app(agent=..., auth=_StubAuth())`. Add one new test:

```python
def test_chat_401_without_auth():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
```

(Uses the real AuthLayer with no Timeplus reachable → `_resolve` raises 401 on the missing header before touching the store.)

- [ ] **Step 6: Run tests, then commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q tests/test_auth_api.py tests/test_server.py tests/test_auth.py
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/auth.py src/tpk/server.py src/tpk/cli.py tests/test_auth_api.py tests/test_server.py
git commit -m "feat: login sessions, /auth router, chat auth gate, eager serve bootstrap"
```

---

### Task 3: Admin-gated management API + /api/users + /api/roles CRUD

**Files:**
- Modify: `src/tpk/api.py`
- Test: `tests/test_api.py` (rework token tests), `tests/test_auth_api.py` (route-coverage additions)

**Interfaces:**
- Consumes: `AuthLayer.require_admin`, Task 1 store functions.
- Produces: `create_api_router(prefix="", auth=None)` — `auth=None` builds `AuthLayer(prefix)` (so existing callers stay valid); all `/api/*` routes carry `admin: User = Depends(auth.require_admin)`.
  - `GET/POST /api/users`, `POST /api/users/update`, `POST /api/users/delete` (bodies below)
  - `GET/POST /api/roles`, `POST /api/roles/delete`
  - User JSON shape: `{"username", "role", "must_change_password", "disabled"}` (never the hash). Role JSON shape: `{"name", "entry_keys", "description"}`.

- [ ] **Step 1: Write failing tests**

Rework `tests/test_api.py`: delete the `X-Admin-Token` tests (`test_admin_token_*` at lines ~107-133 and any `headers={"X-Admin-Token"...}` usage) and the path-entries-403 test's token aspect; the fixture seeds an admin and logs in:

```python
@pytest.fixture
def admin_hdr(c, client, prefix):
    auth.upsert_user(client, auth.User("root", auth.hash_password("adminpass1"), auth.ROLE_ADMIN), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "root", "password": "adminpass1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}
```

Every existing `/api` call in the file gains `headers=admin_hdr`. New tests:

```python
def test_api_requires_admin(c, client, prefix, admin_hdr):
    auth.upsert_role(client, auth.Role("viewer", []), prefix=prefix)
    auth.upsert_user(client, auth.User("eve", auth.hash_password("password-1"), "viewer"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login", json={"username": "eve", "password": "password-1"}).status_code == 200)
    t = c.post("/auth/login", json={"username": "eve", "password": "password-1"}).json()["token"]
    for method, path in [("get", "/api/repos"), ("get", "/api/jobs"), ("get", "/api/users"), ("get", "/api/roles")]:
        assert getattr(c, method)(path).status_code == 401                       # no token
        assert getattr(c, method)(path, headers={"Authorization": f"Bearer {t}"}).status_code == 403  # non-admin


def test_user_crud_via_api(c, admin_hdr):
    r = c.post("/api/users", headers=admin_hdr, json={
        "username": "sam", "password": "password-9", "role": "viewer"})
    assert r.status_code == 400  # role does not exist yet
    assert c.post("/api/roles", headers=admin_hdr, json={
        "name": "viewer", "entry_keys": ["docs@main"], "description": ""}).status_code == 200
    assert c.post("/api/users", headers=admin_hdr, json={
        "username": "sam", "password": "password-9", "role": "viewer"}).status_code == 200
    _eventually(lambda: any(u["username"] == "sam" for u in c.get("/api/users", headers=admin_hdr).json()))
    users = c.get("/api/users", headers=admin_hdr).json()
    sam = next(u for u in users if u["username"] == "sam")
    assert sam["must_change_password"] is True and "password_hash" not in sam
    # disable kills sessions
    _eventually(lambda: c.post("/auth/login", json={"username": "sam", "password": "password-9"}).status_code == 200)
    t = c.post("/auth/login", json={"username": "sam", "password": "password-9"}).json()["token"]
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "sam", "disabled": True}).status_code == 200
    _eventually(lambda: c.get("/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401)


def test_role_reserved_in_use_and_delete(c, admin_hdr):
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "admin", "entry_keys": []}).status_code == 400
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "team", "entry_keys": ["docs@main"]}).status_code == 200
    assert c.post("/api/users", headers=admin_hdr, json={
        "username": "tess", "password": "password-9", "role": "team"}).status_code == 200
    _eventually(lambda: c.post("/api/roles/delete", headers=admin_hdr, json={"name": "team"}).status_code == 409)
    assert c.post("/api/users/delete", headers=admin_hdr, json={"username": "tess"}).status_code == 200
    _eventually(lambda: c.post("/api/roles/delete", headers=admin_hdr, json={"name": "team"}).status_code == 200)


def test_last_admin_protection(c, client, prefix, admin_hdr):
    assert c.post("/api/users/delete", headers=admin_hdr, json={"username": "root"}).status_code == 400
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "root", "disabled": True}).status_code == 400
    assert c.post("/api/roles", headers=admin_hdr, json={"name": "other", "entry_keys": []}).status_code == 200
    assert c.post("/api/users/update", headers=admin_hdr, json={"username": "root", "role": "other"}).status_code == 400
```

Also update the path-entries test: path-type entries now require admin like everything else (the old `TPK_ADMIN_TOKEN` monkeypatching disappears; with `admin_hdr` a path entry succeeds).

- [ ] **Step 2: Run to verify failure**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_api.py`
Expected: FAIL (routes missing, old gate).

- [ ] **Step 3: Implement**

In `src/tpk/api.py`:

1. Delete `_require_admin`, the `import os`/`secrets` usages it needed, every `x_admin_token: str | None = Header(None)` parameter and `_require_admin(...)` call, and the path-entries `TPK_ADMIN_TOKEN` 403 block in `_validate` (with its comment).
2. `create_api_router(prefix: str = "", auth=None)`: `auth = auth or AuthLayer(prefix)` (import from `tpk.auth`), and give every existing route `admin: User = Depends(auth.require_admin)`.
3. Add models + routes:

```python
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
```

```python
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

    @router.get("/users")
    def api_list_users(admin=Depends(auth.require_admin)):
        return [_user_json(u) for u in auth_mod.list_users(_client(), prefix=prefix)]

    @router.post("/users")
    def api_add_user(body: AddUser, admin=Depends(auth.require_admin)):
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
    def api_update_user(body: UpdateUser, admin=Depends(auth.require_admin)):
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
    def api_delete_user(body: DeleteUser, admin=Depends(auth.require_admin)):
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
    def api_list_roles(admin=Depends(auth.require_admin)):
        return [{"name": r.name, "entry_keys": r.entry_keys, "description": r.description}
                for r in auth_mod.list_roles(_client(), prefix=prefix)]

    @router.post("/roles")
    def api_upsert_role(body: UpsertRole, admin=Depends(auth.require_admin)):
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
    def api_delete_role(body: DeleteRole, admin=Depends(auth.require_admin)):
        client = _client()
        if auth_mod.get_role(client, body.name, prefix=prefix) is None:
            raise HTTPException(404, "no such role")
        if auth_mod.usernames_with_role(client, body.name, prefix=prefix):
            raise HTTPException(409, "role is assigned to users")
        auth_mod.delete_role(client, body.name, prefix=prefix)
        return {"ok": True}
```

(`import tpk.auth as auth_mod`, `from fastapi import Depends`.)

- [ ] **Step 4: Run tests, then commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q tests/test_api.py tests/test_auth_api.py
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/api.py tests/test_api.py tests/test_auth_api.py
git commit -m "feat: admin-gated management API with user/role CRUD; drop TPK_ADMIN_TOKEN"
```

---

### Task 4: Chat role scoping through KnowledgeGraph

**Files:**
- Modify: `src/tpk/tools.py`, `src/tpk/server.py`
- Test: `tests/test_tools.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `KnowledgeGraph._corpus_state()` semantics (`None` = unfiltered; `[]` = match nothing via `["__none__"]` sentinel); `auth.get_role`.
- Produces: `tools.ROLE_SCOPE: ContextVar[frozenset[str] | None]` (default `None` = unrestricted). Set by `/chat` for non-admin users; MCP/CLI never set it.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tools.py` (reuse its seeding helpers; `_seed_corpus_entry` and node-seeding exist from the corpus-mgmt work):

```python
from tpk.tools import ROLE_SCOPE


def test_role_scope_restricts_results(kg, ...):  # match existing fixture names
    # seed two enabled entries with one node each: docs@main, internal@v1
    token = ROLE_SCOPE.set(frozenset({"docs@main"}))
    try:
        hits = kg.search_entities("...")
        assert {h["repo"] for h in hits} == {"docs@main"}
        assert kg.list_communities() ... # only docs@main rows
    finally:
        ROLE_SCOPE.reset(token)
    # after reset: both repos visible again
    assert {h["repo"] for h in kg.search_entities("...")} == {"docs@main", "internal@v1"}


def test_role_scope_empty_intersection_matches_nothing(kg, ...):
    token = ROLE_SCOPE.set(frozenset({"absent@v9"}))
    try:
        assert kg.search_entities("...") == []
    finally:
        ROLE_SCOPE.reset(token)


def test_role_scope_applies_without_corpus_store(kg_no_store, ...):
    # kg over a prefix whose kg_repos is empty/absent (corpus state None):
    # scope alone must still filter
    token = ROLE_SCOPE.set(frozenset({"docs@main"}))
    try:
        assert {h["repo"] for h in kg_no_store.search_entities("...")} == {"docs@main"}
    finally:
        ROLE_SCOPE.reset(token)
```

Flesh the `...` seeding/queries out against the file's existing helpers — copy the seeding calls used by `test_disabled_entry_invisible_everywhere` and `test_all_entries_disabled_means_zero_results` (both already in the file); also assert `read_source` refuses a repo outside the scope (raises `ValueError`).

- [ ] **Step 2: Run to verify failure**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_tools.py -k role_scope`
Expected: FAIL (`ROLE_SCOPE` missing).

- [ ] **Step 3: Implement in tools.py**

Top of `src/tpk/tools.py`:

```python
from contextvars import ContextVar

# Per-request cap on which corpus entry keys are queryable. `None` means
# unrestricted (admin chat, MCP server, CLI). Set/reset by the /chat
# handler for non-admin users; langchain-core's executor copies the
# contextvars context into tool threads, so tool calls observe it.
ROLE_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("ROLE_SCOPE", default=None)
```

Add a method on `KnowledgeGraph` and use it everywhere `_corpus_state()` is consumed for filtering (`_nodes_by_ids`, `_edges_touching`, `search_entities`, `list_communities`) and for paths (`read_source`):

```python
    def _scoped_corpus_state(self) -> tuple[list[str] | None, dict[str, Path]]:
        """_corpus_state() with the per-request role scope applied.
        Semantics: corpus None + scope None -> no filter; corpus None +
        scope set -> filter to scope; corpus list + scope None -> corpus
        list; both set -> intersection (possibly empty)."""
        keys, paths = self._corpus_state()
        scope = ROLE_SCOPE.get()
        if scope is None:
            return keys, paths
        if keys is None:
            return sorted(scope), {k: p for k, p in paths.items() if k in scope}
        return ([k for k in keys if k in scope],
                {k: p for k, p in paths.items() if k in scope})
```

Then mechanically replace `self._corpus_state()` with `self._scoped_corpus_state()` at every *filtering* call site (the four query paths + `read_source`'s path resolution). The `active or ["__none__"]` sentinel logic downstream is unchanged and now also covers the empty-intersection case.

Note: with corpus state `None` and a scope set, `paths` is `{}` — `read_source` for scoped users then resolves only via scoped corpus paths; static `repo_paths` fallback must also be restricted: in `read_source`, when `ROLE_SCOPE.get()` is not None, only accept repo keys inside the scope (check the actual `read_source` body and apply the scope check where it merges `self.repo_paths` with corpus paths).

- [ ] **Step 4: Set the scope in /chat**

In `src/tpk/server.py` `chat` (inside `stream()` so it wraps the whole agent run; `from tpk.tools import ROLE_SCOPE`, `import tpk.auth as auth_mod`):

```python
        scope = None
        if user.role != auth_mod.ROLE_ADMIN:
            try:
                role = auth_mod.get_role(auth._client(), user.role, prefix=stream_prefix)
            except Exception:
                role = None
            scope = frozenset(role.entry_keys) if role else frozenset()

        async def stream():
            token = ROLE_SCOPE.set(scope) if scope is not None else None
            try:
                ...existing body...
            finally:
                if token is not None:
                    ROLE_SCOPE.reset(token)
```

(A missing/unreadable role fails closed: `frozenset()` = empty scope = tools see nothing.)

Add to `tests/test_server.py` (stub auth with a non-admin user; FakeAgent doesn't call tools, so assert the ContextVar is scoped-and-reset around the stream):

```python
def test_chat_sets_and_resets_role_scope(client_with_nonadmin_stub, ...):
    # non-admin stub user with role "viewer"; no kg_roles row reachable ->
    # scope falls back to frozenset() (fail closed); after response,
    # ROLE_SCOPE.get() is None again in this thread.
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    from tpk.tools import ROLE_SCOPE
    assert ROLE_SCOPE.get() is None
```

- [ ] **Step 5: Run tests, then commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q tests/test_tools.py tests/test_server.py
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/tools.py src/tpk/server.py tests/test_tools.py tests/test_server.py
git commit -m "feat: role-scoped chat via ROLE_SCOPE contextvar in KnowledgeGraph"
```

---

### Task 5: Web UI — login gate, forced change, Users tab, bearer everywhere

**Files:**
- Create: `web/src/api.ts`, `web/src/Login.tsx`, `web/src/Users.tsx`
- Modify: `web/src/App.tsx`, `web/src/Manage.tsx`

**Interfaces:**
- Consumes: `/auth/*` endpoints (Task 2 shapes), `/api/users` + `/api/roles` (Task 3 shapes), `/api/repos` for the entry-key picker.
- Produces: `api.ts` exports `getToken()`, `setToken(t: string | null)`, `apiFetch(path: string, init?: RequestInit): Promise<Response>` (adds bearer; on 401 clears token and calls the registered `onUnauthorized` handler), `onUnauthorized(cb: () => void)`.

- [ ] **Step 1: api.ts**

```typescript
let unauthorized: (() => void) | null = null;
export function onUnauthorized(cb: () => void) { unauthorized = cb; }
export function getToken(): string | null { return sessionStorage.getItem("tpk_token"); }
export function setToken(t: string | null) {
  if (t) sessionStorage.setItem("tpk_token", t);
  else sessionStorage.removeItem("tpk_token");
}
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401) { setToken(null); unauthorized?.(); }
  return resp;
}
```

- [ ] **Step 2: Login.tsx**

One component with two modes: login form, and forced password-change form (shown when `must_change_password`). On login success: `setToken(token)`; if `must_change_password` → change mode; change calls `/auth/change-password` then `onDone()`. Follow `Manage.tsx`'s styling (existing `app.css` variables, same input/button classes). Errors render the response `detail` string; the forced-change screen states: "You must change your password before continuing." Props: `{ onDone(me: {username: string; role: string}) }`. Fetch `/auth/me` after login/change to hand `me` up.

- [ ] **Step 3: Users.tsx (admin tab)**

Two sections, same table styling as Manage.tsx:

- **Users**: rows `username / role (select over roles + "admin") / disabled toggle / reset-password button / delete button (confirm)`. "Add user" row: username, initial password, role select, `must_change_password` checkbox (default on). Calls: `GET /api/users`, `POST /api/users`, `POST /api/users/update`, `POST /api/users/delete`. Render API error `detail` strings inline (they carry last-admin/role-missing messages).
- **Roles**: rows `name / entry-key checkboxes / description / save / delete (confirm)`. Entry-key options come from `GET /api/repos` (`entry_key` field of each row). Calls: `GET /api/roles`, `POST /api/roles`, `POST /api/roles/delete`; 409 on delete renders "role is assigned to users".
- Poll nothing; refetch after each mutation. Use `apiFetch` exclusively.

- [ ] **Step 4: App.tsx + Manage.tsx wiring**

- `App.tsx`: state `me: {username, role} | null`. On mount: if `getToken()`, `GET /auth/me` → set `me` (a `must_change_password: true` answer routes to the Login component's change mode). Register `onUnauthorized(() => setMe(null))`. Render `<Login onDone={setMe}/>` when `me === null`. View switcher gains `"users"`; the Manage and Users tab buttons render only when `me.role === "admin"`. Header shows `me.username` and a Logout button (`POST /auth/logout` via `apiFetch`, then `setToken(null); setMe(null)`).
- `App.tsx`'s chat `send()` switches its raw `fetch("/chat", ...)` to `apiFetch`.
- `Manage.tsx`: delete the token input + `sessionStorage tpk_admin_token` block and the `X-Admin-Token` header logic; every `fetch` becomes `apiFetch`.

- [ ] **Step 5: Build + sanitize + commit**

```bash
cd web && npm run build && npm run check:sanitize && cd ..
git add web/src
git commit -m "feat: login gate, forced password change, admin users/roles tab"
```

---

### Task 6: timeplusd DB user for the compose stack

**Files:**
- Create: `deploy/docker/tpk-entrypoint.sh`
- Modify: `deploy/docker/Dockerfile`, `docker-compose.yml`, `.env.example`

**Interfaces:**
- Consumes: base image entrypoint `/entrypoint.sh` (verified: `Entrypoint=["/entrypoint.sh"]`, users config is YAML at `/etc/timeplusd-server/users.yaml` with an empty `users.d/`).
- Produces: compose services connect as DB user `tpk`; `default` DB user gets the same password (scalar YAML override — reliable merge semantics; loopback-only `networks` list overrides merge unreliably in YAML configs, so password-protecting `default` is the mechanism; the spec's intent — `default` stops being an open door on the published 8123 — holds).

- [ ] **Step 1: Entrypoint wrapper**

`deploy/docker/tpk-entrypoint.sh`:

```bash
#!/bin/bash
# Renders a users.d override from TIMEPLUS_PASSWORD before starting
# timeplusd. When the var is unset (bare dev container), nothing is
# rendered and the base image's open `default` user is kept.
set -euo pipefail

if [ -n "${TIMEPLUS_PASSWORD:-}" ]; then
  hash=$(printf %s "$TIMEPLUS_PASSWORD" | sha256sum | awk '{print $1}')
  cat > /etc/timeplusd-server/users.d/tpk-users.yaml <<EOF
users:
    tpk:
        password_sha256_hex: ${hash}
        profile: default
        quota: default
        networks:
            ip: "::/0"
    default:
        password_sha256_hex: ${hash}
EOF
fi
exec /entrypoint.sh "$@"
```

- [ ] **Step 2: Dockerfile**

After the existing COPY lines in `deploy/docker/Dockerfile`:

```dockerfile
COPY deploy/docker/tpk-entrypoint.sh /usr/local/bin/tpk-entrypoint.sh
RUN chmod +x /usr/local/bin/tpk-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/tpk-entrypoint.sh"]
```

(Confirm the base image's CMD is null — verified — so no CMD passthrough is needed; the `agent` service's compose `entrypoint: ["tpk"]` override is unaffected.)

- [ ] **Step 3: Compose + env**

`docker-compose.yml`:
- `tpk` service `environment`: add `TIMEPLUS_PASSWORD: ${TIMEPLUS_PASSWORD:?TIMEPLUS_PASSWORD must be set in .env}`, `TIMEPLUS_USER: tpk` (config.py reads both — `src/tpk/config.py:24-25`).
- `agent` service `environment`: add the same two lines.
- Remove the `TPK_ADMIN_TOKEN` line from the `agent` service and the `TPK_ADMIN_TOKEN` entry in the header comment block; add a `TIMEPLUS_PASSWORD` line to the header comment.

`.env.example`: remove the `TPK_ADMIN_TOKEN` block; add:

```
# Password for the stack's timeplusd. Required by docker compose: provisions
# the `tpk` DB user and locks the `default` user with the same password.
TIMEPLUS_PASSWORD=change-this-db-password
```

- [ ] **Step 4: Verify locally and commit**

```bash
docker compose build tpk
TIMEPLUS_PASSWORD=testpw docker compose up -d tpk && sleep 8
curl -s "http://localhost:8123/" -u default: --data "SELECT 1"        # expect auth error
curl -s "http://localhost:8123/" -u tpk:testpw --data "SELECT 1"      # expect 1
curl -s "http://localhost:8123/" -u default:testpw --data "SELECT 1"  # expect 1
git add deploy/docker/tpk-entrypoint.sh deploy/docker/Dockerfile docker-compose.yml .env.example
git commit -m "feat: dedicated tpk DB user; password-lock default in compose"
```

(If the running dev timeplusd container occupies 8123, stop it first; restore it after.)

---

### Task 7: Docs + live E2E + PR

**Files:**
- Modify: `README.md`
- No new tests; this is the live gate.

- [ ] **Step 1: README**

- New "Users & roles" subsection under "Manage the corpus": login model, seeded `admin`/`changeme` + forced change, roles as `name@ref` access lists (admin reserved), user/role API endpoints with bearer auth, break-glass (locked-out operator: `DELETE FROM kg_users WHERE 1=1` via direct SQL — the API protects the last admin — then restart to re-seed), session TTL env.
- Remove every `TPK_ADMIN_TOKEN`/`X-Admin-Token` mention (Ingest section, Manage section, API table); the API table's auth column becomes "admin bearer token".
- Setup/compose docs: `TIMEPLUS_PASSWORD` now required; DB user note (services connect as `tpk`; `default` shares the password; dev outside compose unchanged).

- [ ] **Step 2: Rebuild and run the stack from THIS branch**

```bash
docker compose build && docker compose up -d && sleep 10
curl -s http://localhost:8000/healthz
```

- [ ] **Step 3: Live E2E**

```bash
# seeded admin + forced change
curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"changeme"}'          # token + must_change_password=true
# /api blocked until change (expect 403 password_change_required), then change:
curl -s -X POST localhost:8000/auth/change-password -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"old_password":"changeme","new_password":"<real-pw>"}'
# role + user
curl -s -X POST localhost:8000/api/roles -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"name":"docs-only","entry_keys":["docs@main"]}'
curl -s -X POST localhost:8000/api/users -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"username":"cust","password":"cust-pass-1","role":"docs-only","must_change_password":false}'
# login as cust; /api/repos must 403; /chat must answer FROM docs ONLY:
# ask a question that admin-chat answers with proton-enterprise citations and
# verify cust-chat does not cite proton-enterprise (and can still cite docs).
# unauthenticated /chat and /api must 401. UI: browser login flow, forced
# change, Users tab CRUD, Manage visible only for admin (Playwright).
# DB user: curl -s localhost:8123 -u default: --data "SELECT 1" -> auth error;
#          -u tpk:$TIMEPLUS_PASSWORD -> 1.
```

Record each result. Judge: seeded login works; forced change enforced then cleared; role-scoped chat provably narrower than admin chat on the same question; 401/403 matrix correct; DB default user rejected without password.

- [ ] **Step 4: Full suite + UI checks one final time**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q && cd web && npm run build && npm run check:sanitize && cd ..
```

- [ ] **Step 5: Commit docs, push branch, open PR (do NOT merge)**

```bash
git add README.md
git commit -m "docs: users & roles, DB credential setup, admin-token removal"
git push -u origin <branch>
gh pr create --repo timeplus-io/timeplus-knowledge --base main \
  --title "feat: authentication and role-based repo access" \
  --body "Closes #8. Closes #6 (eager serve bootstrap). ... (summarize: auth store, /auth router, admin-gated API + user/role CRUD, role-scoped chat, login UI + Users tab, tpk DB user; include E2E evidence; note the default-user-password deviation from the spec's loopback wording and why)"
```

---

## After this plan

- Login rate limiting / lockout (spec non-goal).
- Scheduled session cleanup (expired rows are lazily deleted only on access).
- `body.github` field validation (pre-existing gap, noted in corpus-mgmt review).
