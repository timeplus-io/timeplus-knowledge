# Remote MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the six knowledge-graph MCP tools over authenticated streamable HTTP at `/mcp` in the `tpk serve` app, so a coding agent connects with `claude mcp add --transport http … --header "Authorization: Bearer tpk_…"` and gets exactly the token owner's capabilities and role corpus scope.

**Architecture:** A new per-user API-token store (`kg_api_tokens`) next to sessions in `auth.py`. `mcp_server.build_server(kg, guard=None)` gains an optional guard; `mcp_http.py` supplies the guard (role scope + `source:view`) and an ASGI auth wrapper, and `create_app()` adds it as a Starlette `Route("/mcp")` with the SDK session manager run in the FastAPI lifespan. REST endpoints + a "API tokens" UI page manage tokens.

**Tech Stack:** Python 3.11, FastAPI/Starlette, `mcp` SDK 2.0 (`mcp.server.mcpserver.MCPServer`), Timeplus mutable streams via `timeplus_connect`, React + TypeScript (Vite), pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-09-19-remote-mcp-design.md` (issue #74). Branch: `feat/remote-mcp`.

## Global Constraints

- Token plaintext format: `tpk_` + `secrets.token_urlsafe(32)`; only the sha256 hex hash is stored; plaintext is returned exactly once (create response).
- API tokens are valid **only** at `/mcp`. `AuthLayer._resolve` (REST) stays session-only. Session tokens are rejected at `/mcp`.
- Access gate for `/mcp` and for managing one's own tokens: existing `explore` capability (`auth.CAP_EXPLORE`). No new capability.
- Expiry choices: omitted (never) or exactly one of `30`, `90`, `365` days.
- Per-user cap: 20 live tokens → HTTP 409.
- `/mcp` status codes: 401 (missing/invalid/expired/revoked token, disabled/missing user) with header `WWW-Authenticate: Bearer`; 403 (`password_change_required`, `missing capability: explore`); 503 (auth store unreachable). Fail closed.
- Non-admin scope: `frozenset(role.entry_keys)`, or the empty frozenset when the role is missing/unreadable. Admin: no scope (`None`).
- `ROLE_SCOPE` must be set **inside** the function run in the threadpool (the thread that executes the KG query), and reset in `finally`.
- stdio MCP (`tpk-mcp`, `python -m tpk.mcp_server`) stays unauthenticated and unscoped; `tests/test_mcp_server.py` must stay green unchanged.
- Config keys use `config.setting()` (env > file > default): `TPK_MCP_HTTP_ENABLED` / `[server].mcp_http` (default `true`); `TPK_MCP_ALLOWED_HOSTS` / `[server].mcp_allowed_hosts` (comma-separated, default empty = DNS-rebinding protection off).
- The codebase uses no nullable columns. "No expiry" / "never used" are stored as the epoch sentinel `1970-01-01T00:00:00Z` and surfaced as `None` / JSON `null`.
- Integration tests need a running timeplusd: run with `TIMEPLUS_HOST=localhost uv run pytest …` (they skip otherwise). Mutable-stream writes lag ~100–300 ms; poll with an `_eventually` helper, never `sleep` a fixed time.
- Commit messages end with: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `src/tpk/db.py` (modify) | `kg_api_tokens` DDL in `ensure_schema`; add to `drop_schema`. |
| `src/tpk/auth.py` (modify) | `ApiToken` dataclass + token store helpers; `resolve_scope()`. |
| `src/tpk/graph_api.py` (modify) | Use `auth.resolve_scope()` instead of inline logic. |
| `src/tpk/api.py` (modify) | `/api/tokens*` endpoints; delete tokens on user delete/disable. |
| `src/tpk/mcp_server.py` (modify) | `build_server(kg, guard=None)`; async tool handlers taking `Context`. |
| `src/tpk/mcp_http.py` (create) | `enabled()`, `McpAuth` ASGI wrapper, `make_guard()`, `create_mcp_route()`. |
| `src/tpk/server.py` (modify) | Lifespan + `/mcp` route wiring. |
| `web/src/Tokens.tsx` (create) | "API tokens" page. |
| `web/src/Shell.tsx`, `web/src/App.tsx` (modify) | New `tokens` view + nav item. |
| `web/src/Users.tsx` (modify) | Per-user "Revoke API tokens". |
| `tests/test_api_tokens.py`, `tests/test_tokens_api.py`, `tests/test_mcp_http.py` (create) | Store, REST, and `/mcp` tests. |
| `tests/conftest.py` (modify) | Add `kg_api_tokens` to `_KG_STREAMS`. |
| `README.md`, `docs/FEATURES.md`, `deploy/k8s/README.md`, `.env.example`, `repos.toml`, `deploy/docker/repos.container.toml` (modify) | Docs + config samples. |

---

### Task 1: API token store

**Files:**
- Modify: `src/tpk/db.py` (after the `kg_sessions` DDL ~line 194; `drop_schema` list ~line 235)
- Modify: `src/tpk/auth.py` (new section after the sessions section, before `_session_ttl`)
- Modify: `tests/conftest.py` (`_KG_STREAMS`)
- Test: `tests/test_api_tokens.py` (create)

**Interfaces:**
- Consumes: `db._keyed_stream`, `db.qualified`, `db.latest`, `db.delete`, `auth._now`, `auth._token_hash`.
- Produces (all in `tpk.auth`):
  - `API_TOKEN_PREFIX = "tpk_"`, `MAX_API_TOKENS_PER_USER = 20`, `API_TOKEN_EXPIRY_DAYS = (30, 90, 365)`
  - `class ApiTokenLimitError(Exception)`
  - `@dataclass ApiToken(token_id: str, username: str, name: str, hint: str, created_at: datetime, expires_at: datetime | None, last_used_at: datetime | None)`
  - `create_api_token(client, username: str, name: str, expires_days: int | None = None, prefix: str = "") -> tuple[str, ApiToken]` — raises `ValueError` (bad name/expiry), `ApiTokenLimitError`.
  - `list_api_tokens(client, username: str, prefix: str = "") -> list[ApiToken]` — live (non-expired) tokens, oldest first.
  - `resolve_api_token(client, token: str, prefix: str = "") -> ApiToken | None` — `None` for unknown/expired/non-`tpk_`; deletes expired; bumps `last_used_at` when older than 5 minutes.
  - `revoke_api_token(client, username: str, token_id: str, prefix: str = "") -> bool`
  - `delete_user_api_tokens(client, username: str, prefix: str = "") -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_tokens.py`:

```python
import time
from datetime import timedelta

import pytest

from conftest import requires_timeplus
from tpk import auth, db

pytestmark = requires_timeplus


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def test_create_returns_plaintext_once_and_stores_only_hash(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    assert token.startswith("tpk_") and rec.hint == token[-4:]
    assert rec.expires_at is None and rec.last_used_at is None
    rows = _eventually(lambda: client.query(
        f"SELECT token_hash FROM {db.latest(db.qualified('kg_api_tokens', prefix))}").result_rows)
    assert rows and rows[0][0] != token and token not in str(rows)


def test_resolve_valid_unknown_and_non_prefixed(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    got = _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    assert got.token_id == rec.token_id and got.username == "alice"
    assert auth.resolve_api_token(client, "tpk_nope", prefix=prefix) is None
    assert auth.resolve_api_token(client, "not-prefixed", prefix=prefix) is None


def test_resolve_bumps_last_used_throttled(tp):
    client, prefix = tp
    token, _ = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    first = _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix)[0].last_used_at)
    assert first is not None
    auth.resolve_api_token(client, token, prefix=prefix)  # within 5 min: no rewrite
    time.sleep(0.5)
    assert auth.list_api_tokens(client, "alice", prefix=prefix)[0].last_used_at == first


def test_expiry(tp, monkeypatch):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "ci", expires_days=30, prefix=prefix)
    assert rec.expires_at is not None
    _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix))
    real_now = auth._now
    monkeypatch.setattr(auth, "_now", lambda: real_now() + timedelta(days=31))
    assert auth.resolve_api_token(client, token, prefix=prefix) is None
    assert auth.list_api_tokens(client, "alice", prefix=prefix) == []


def test_validation(tp):
    client, prefix = tp
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "", prefix=prefix)
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "x" * 65, prefix=prefix)
    with pytest.raises(ValueError):
        auth.create_api_token(client, "alice", "ok", expires_days=7, prefix=prefix)


def test_revoke_is_owner_scoped(tp):
    client, prefix = tp
    token, rec = auth.create_api_token(client, "alice", "laptop", prefix=prefix)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix))
    assert auth.revoke_api_token(client, "bob", rec.token_id, prefix=prefix) is False
    assert auth.revoke_api_token(client, "alice", rec.token_id, prefix=prefix) is True
    assert _eventually(lambda: auth.resolve_api_token(client, token, prefix=prefix),
                       predicate=lambda v: v is None) is None


def test_delete_user_tokens_and_cap(tp, monkeypatch):
    client, prefix = tp
    monkeypatch.setattr(auth, "MAX_API_TOKENS_PER_USER", 2)
    auth.create_api_token(client, "alice", "a", prefix=prefix)
    auth.create_api_token(client, "alice", "b", prefix=prefix)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                predicate=lambda v: len(v) == 2)
    with pytest.raises(auth.ApiTokenLimitError):
        auth.create_api_token(client, "alice", "c", prefix=prefix)
    auth.delete_user_api_tokens(client, "alice", prefix=prefix)
    assert _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                       predicate=lambda v: v == []) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_api_tokens.py -q`
Expected: FAIL — `AttributeError: module 'tpk.auth' has no attribute 'create_api_token'`.

- [ ] **Step 3: Add the stream**

In `src/tpk/db.py`, directly after the `kg_sessions` `client.command(...)` block in `ensure_schema`:

```python
    # Long-lived per-user API tokens for the remote MCP endpoint (#74). No
    # nullable columns in this schema: expires_at / last_used_at use the epoch
    # (1970-01-01) as the "never" sentinel -- see auth._NEVER.
    client.command(_keyed_stream(prefix, "kg_api_tokens", [
        "token_hash string", "token_id string", "username string",
        "name string", "hint string",
        "created_at datetime64(3, 'UTC')", "expires_at datetime64(3, 'UTC')",
        "last_used_at datetime64(3, 'UTC')",
    ], pk="token_hash"))
```

In `drop_schema`, add `"kg_api_tokens"` to the tuple (after `"kg_sessions"`). In `tests/conftest.py`, add `"kg_api_tokens"` to `_KG_STREAMS` (after `"kg_sessions"`).

- [ ] **Step 4: Implement the helpers**

In `src/tpk/auth.py`, after `delete_user_sessions` and before the `# -- HTTP layer` comment:

```python
# -- API tokens (remote MCP, #74) -------------------------------------------
# Long-lived per-user credentials, valid ONLY at /mcp (the REST API stays
# session-only). Stored like sessions: sha256 of the plaintext as the key,
# the plaintext returned exactly once at creation.

API_TOKEN_PREFIX = "tpk_"
MAX_API_TOKENS_PER_USER = 20
API_TOKEN_EXPIRY_DAYS = (30, 90, 365)
_TOUCH_INTERVAL = timedelta(minutes=5)
# "never" sentinel for expires_at / last_used_at (no nullable columns).
_NEVER = datetime(1970, 1, 1, tzinfo=timezone.utc)
_API_TOKEN_COLUMNS = ["token_hash", "token_id", "username", "name", "hint",
                      "created_at", "expires_at", "last_used_at"]


class ApiTokenLimitError(Exception):
    """The user already holds MAX_API_TOKENS_PER_USER live tokens."""


@dataclass
class ApiToken:
    token_id: str
    username: str
    name: str
    hint: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


def _from_db(dt: datetime) -> datetime | None:
    dt = dt.replace(tzinfo=timezone.utc)
    return None if dt.year == 1970 else dt


def _api_token_rows(client, where: str, params: dict, prefix: str):
    return client.query(
        f"SELECT {', '.join(_API_TOKEN_COLUMNS)} FROM "
        f"{db.latest(db.qualified('kg_api_tokens', prefix))} WHERE {where}"
        f" ORDER BY created_at",
        parameters=params,
    ).result_rows


def _row_to_api_token(row) -> ApiToken:
    _hash, token_id, username, name, hint, created_at, expires_at, last_used_at = row
    return ApiToken(token_id, username, name, hint,
                    created_at.replace(tzinfo=timezone.utc),
                    _from_db(expires_at), _from_db(last_used_at))


def _live(t: ApiToken) -> bool:
    return t.expires_at is None or t.expires_at >= _now()


def list_api_tokens(client, username: str, prefix: str = "") -> list[ApiToken]:
    rows = _api_token_rows(client, "username = %(u)s", {"u": username}, prefix)
    return [t for t in map(_row_to_api_token, rows) if _live(t)]


def create_api_token(client, username: str, name: str,
                     expires_days: int | None = None,
                     prefix: str = "") -> tuple[str, ApiToken]:
    name = (name or "").strip()
    if not 1 <= len(name) <= 64:
        raise ValueError("token name must be 1-64 characters")
    if expires_days is not None and expires_days not in API_TOKEN_EXPIRY_DAYS:
        raise ValueError("expires_days must be one of 30, 90, 365")
    if len(list_api_tokens(client, username, prefix=prefix)) >= MAX_API_TOKENS_PER_USER:
        raise ApiTokenLimitError(f"at most {MAX_API_TOKENS_PER_USER} API tokens per user")
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    expires_at = now + timedelta(days=expires_days) if expires_days else None
    rec = ApiToken(secrets.token_hex(6), username, name, token[-4:], now, expires_at, None)
    client.insert(
        db.qualified("kg_api_tokens", prefix),
        [[_token_hash(token), rec.token_id, username, name, rec.hint,
          now, expires_at or _NEVER, _NEVER]],
        column_names=_API_TOKEN_COLUMNS,
    )
    return token, rec


def resolve_api_token(client, token: str, prefix: str = "") -> ApiToken | None:
    if not token.startswith(API_TOKEN_PREFIX):
        return None
    h = _token_hash(token)
    rows = _api_token_rows(client, "token_hash = %(h)s", {"h": h}, prefix)
    if not rows:
        return None
    rec = _row_to_api_token(rows[0])
    stream = db.qualified("kg_api_tokens", prefix)
    if not _live(rec):
        db.delete(client, stream, "token_hash = %(h)s", {"h": h}, ("token_hash",))
        return None
    now = _now()
    # Throttled: a busy agent must not rewrite the row on every call.
    if rec.last_used_at is None or now - rec.last_used_at > _TOUCH_INTERVAL:
        client.insert(stream, [[h, rec.token_id, rec.username, rec.name, rec.hint,
                                rec.created_at, rec.expires_at or _NEVER, now]],
                      column_names=_API_TOKEN_COLUMNS)
        rec.last_used_at = now
    return rec


def revoke_api_token(client, username: str, token_id: str, prefix: str = "") -> bool:
    params = {"u": username, "i": token_id}
    where = "username = %(u)s AND token_id = %(i)s"
    if not _api_token_rows(client, where, params, prefix):
        return False
    db.delete(client, db.qualified("kg_api_tokens", prefix), where, params, ("token_hash",))
    return True


def delete_user_api_tokens(client, username: str, prefix: str = "") -> None:
    db.delete(client, db.qualified("kg_api_tokens", prefix),
              "username = %(u)s", {"u": username}, ("token_hash",))
```

- [ ] **Step 5: Run the tests**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_api_tokens.py tests/test_auth.py tests/test_db.py -q`
Expected: all PASS. If `db.delete` rejects the multi-column `where` (read `db.delete` lines 66–80: it selects the `pk` columns matching `where`, then deletes by pk), keep `pk=("token_hash",)` — that is already what the code above passes.

- [ ] **Step 6: Commit**

```bash
git add src/tpk/db.py src/tpk/auth.py tests/conftest.py tests/test_api_tokens.py
git commit -m "feat(auth): per-user API token store for remote MCP (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Shared `resolve_scope` helper

**Files:**
- Modify: `src/tpk/auth.py` (after `effective_capabilities`)
- Modify: `src/tpk/graph_api.py:14-31` (`_scoped`)
- Test: `tests/test_capabilities.py` (append)

**Interfaces:**
- Produces: `auth.resolve_scope(client, user: User, prefix: str = "") -> frozenset[str] | None` — `None` for admin; role's `entry_keys` as a frozenset; **empty** frozenset when the role is missing or the read raises.

- [ ] **Step 1: Write the failing tests** (infra-free; append to `tests/test_capabilities.py`)

```python
def test_resolve_scope_admin_is_unscoped():
    from tpk import auth
    assert auth.resolve_scope(object(), auth.User("root", "h", auth.ROLE_ADMIN)) is None


def test_resolve_scope_fails_closed_when_role_unreadable():
    from tpk import auth

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("store down")

    assert auth.resolve_scope(Boom(), auth.User("u", "h", "support")) == frozenset()


def test_resolve_scope_uses_role_entry_keys(monkeypatch):
    from tpk import auth
    monkeypatch.setattr(auth, "get_role",
                        lambda client, name, prefix="": auth.Role(name, ["alpha@v1", "beta@v2"]))
    assert auth.resolve_scope(object(), auth.User("u", "h", "support")) == frozenset({"alpha@v1", "beta@v2"})


def test_resolve_scope_missing_role_is_empty(monkeypatch):
    from tpk import auth
    monkeypatch.setattr(auth, "get_role", lambda client, name, prefix="": None)
    assert auth.resolve_scope(object(), auth.User("u", "h", "gone")) == frozenset()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_capabilities.py -q -k resolve_scope`
Expected: FAIL — `AttributeError: … 'resolve_scope'`.

- [ ] **Step 3: Implement**

In `src/tpk/auth.py`, directly after `effective_capabilities`:

```python
def resolve_scope(client, user: User, prefix: str = "") -> frozenset[str] | None:
    """The corpus scope to apply to a user's graph queries: None for admin
    (unrestricted), else the role's exact `name@ref` entry keys. Fails closed
    to the EMPTY scope when the role is missing or unreadable. Shared by the
    Explorer API (graph_api) and the remote MCP guard (mcp_http)."""
    if user.role == ROLE_ADMIN:
        return None
    try:
        role = get_role(client, user.role, prefix=prefix)
    except Exception:
        role = None
    return frozenset(role.entry_keys) if role else frozenset()
```

In `src/tpk/graph_api.py`, replace the whole `_scoped` function with:

```python
    async def _scoped(user, fn):
        """Resolve the caller's role scope (fail-closed, see
        auth.resolve_scope) and run the blocking KG call under it, off the
        event loop -- mirrors server.py's /chat handler."""
        def _scope():
            try:
                return auth_mod.resolve_scope(auth._client(), user, prefix=prefix)
            except Exception:  # auth._client() itself failed
                return None if user.role == auth_mod.ROLE_ADMIN else frozenset()

        scope = await run_in_threadpool(_scope)
        token = ROLE_SCOPE.set(scope) if scope is not None else None
        try:
            return await run_in_threadpool(fn)
        finally:
            if token is not None:
                ROLE_SCOPE.reset(token)
```

- [ ] **Step 4: Run the tests**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_capabilities.py tests/test_graph_api.py -q`
Expected: all PASS (graph API behaviour unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/tpk/auth.py src/tpk/graph_api.py tests/test_capabilities.py
git commit -m "refactor(auth): shared fail-closed resolve_scope for graph API + MCP (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Token REST API + user lifecycle

**Files:**
- Modify: `src/tpk/api.py` (models near line 95; endpoints after `/users/delete`; hooks in `api_update_user` ~line 412 and `api_delete_user` ~line 427)
- Test: `tests/test_tokens_api.py` (create)

**Interfaces:**
- Consumes: Task 1 helpers; `auth.require_cap`, `auth.effective_caps`, `_guard_admin_target`, `_client()`, `prefix` (all already in `create_api_router`'s closure).
- Produces (HTTP, all under `/api`):
  - `GET /tokens[?username=X]` → `{"tokens": [TokenJSON]}`
  - `POST /tokens` `{name, expires_days?}` → `TokenJSON + {"token": "tpk_…"}`
  - `POST /tokens/revoke` `{token_id, username?}` → `{"ok": true}` | 404
  - `POST /tokens/revoke-all` `{username}` → `{"ok": true}`
  - `TokenJSON = {token_id, username, name, hint, created_at, expires_at|null, last_used_at|null}` (ISO-8601 strings).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tokens_api.py`:

```python
import time

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk import auth
from tpk.server import create_app

pytestmark = requires_timeplus


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


@pytest.fixture()
def env(tp):
    client, prefix = tp
    c = TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix))
    auth.upsert_role(client, auth.Role("explorer", ["alpha@v1"], capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_role(client, auth.Role("chatter", [], capabilities=[auth.CAP_CHAT]), prefix=prefix)
    auth.upsert_role(client, auth.Role("manager", ["alpha@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_USERS_MANAGE]), prefix=prefix)
    for name, role in [("alice", "explorer"), ("bob", "explorer"), ("carl", "chatter"),
                       ("mia", "manager"), ("root", auth.ROLE_ADMIN)]:
        auth.upsert_user(client, auth.User(name, auth.hash_password("password-1"), role), prefix=prefix)

    def hdr(name):
        login = lambda: c.post("/auth/login", json={"username": name, "password": "password-1"})
        _eventually(lambda: login().status_code == 200)
        return {"Authorization": f"Bearer {login().json()['token']}"}

    return c, client, prefix, hdr


def _create(c, h, name="laptop", **extra):
    return c.post("/api/tokens", headers=h, json={"name": name, **extra})


def test_create_list_revoke_own(env):
    c, _, _, hdr = env
    h = hdr("alice")
    r = _create(c, h, expires_days=30)
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("tpk_") and body["hint"] == body["token"][-4:]
    assert body["expires_at"] is not None and body["last_used_at"] is None
    listed = _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"])
    assert [t["token_id"] for t in listed] == [body["token_id"]]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    assert c.post("/api/tokens/revoke", headers=h, json={"token_id": body["token_id"]}).status_code == 200
    assert _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"],
                       predicate=lambda v: v == []) == []
    assert c.post("/api/tokens/revoke", headers=h, json={"token_id": "nope"}).status_code == 404


def test_requires_explore_and_valid_input(env):
    c, _, _, hdr = env
    assert c.get("/api/tokens").status_code == 401
    assert _create(c, hdr("carl")).status_code == 403
    h = hdr("alice")
    assert _create(c, h, name="").status_code == 400
    assert _create(c, h, expires_days=7).status_code == 400


def test_other_users_tokens_need_users_manage(env):
    c, _, _, hdr = env
    a, b, m = hdr("alice"), hdr("bob"), hdr("mia")
    tid = _create(c, a).json()["token_id"]
    _eventually(lambda: c.get("/api/tokens", headers=a).json()["tokens"])
    assert c.get("/api/tokens", params={"username": "alice"}, headers=b).status_code == 403
    assert c.post("/api/tokens/revoke", headers=b, json={"token_id": tid, "username": "alice"}).status_code == 403
    assert c.post("/api/tokens/revoke-all", headers=b, json={"username": "alice"}).status_code == 403
    assert [t["token_id"] for t in c.get("/api/tokens", params={"username": "alice"}, headers=m).json()["tokens"]] == [tid]
    assert c.post("/api/tokens/revoke-all", headers=m, json={"username": "alice"}).status_code == 200
    assert _eventually(lambda: c.get("/api/tokens", headers=a).json()["tokens"],
                       predicate=lambda v: v == []) == []
    # a non-admin manager may not touch an admin's tokens
    assert c.post("/api/tokens/revoke-all", headers=m, json={"username": "root"}).status_code == 403


def test_cap_409(env, monkeypatch):
    c, _, _, hdr = env
    monkeypatch.setattr(auth, "MAX_API_TOKENS_PER_USER", 1)
    h = hdr("alice")
    assert _create(c, h).status_code == 200
    _eventually(lambda: c.get("/api/tokens", headers=h).json()["tokens"])
    assert _create(c, h, name="second").status_code == 409


def test_disable_and_delete_user_drop_tokens(env):
    c, client, prefix, hdr = env
    a, r = hdr("alice"), hdr("root")
    _create(c, a)
    _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix))
    assert c.post("/api/users/update", headers=r, json={"username": "alice", "disabled": True}).status_code == 200
    assert _eventually(lambda: auth.list_api_tokens(client, "alice", prefix=prefix),
                       predicate=lambda v: v == []) == []
    b = hdr("bob")
    _create(c, b)
    _eventually(lambda: auth.list_api_tokens(client, "bob", prefix=prefix))
    assert c.post("/api/users/delete", headers=r, json={"username": "bob"}).status_code == 200
    assert _eventually(lambda: auth.list_api_tokens(client, "bob", prefix=prefix),
                       predicate=lambda v: v == []) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tokens_api.py -q`
Expected: FAIL with 404/405 on `/api/tokens`.

- [ ] **Step 3: Implement the endpoints**

In `src/tpk/api.py`, add models after `class DeleteRole`:

```python
class CreateToken(BaseModel):
    name: str
    expires_days: int | None = None


class RevokeToken(BaseModel):
    token_id: str
    username: str | None = None   # another user's token: needs users:manage


class RevokeAllTokens(BaseModel):
    username: str
```

Inside `create_api_router`, after the `/users/delete` endpoint:

```python
    # -- API tokens (remote MCP, #74) ---------------------------------------
    def _token_json(t) -> dict:
        iso = lambda d: d.isoformat() if d else None
        return {"token_id": t.token_id, "username": t.username, "name": t.name,
                "hint": t.hint, "created_at": iso(t.created_at),
                "expires_at": iso(t.expires_at), "last_used_at": iso(t.last_used_at)}

    def _token_owner(client, actor: User, username: str | None) -> str:
        """The user whose tokens this call targets. Anyone (with `explore`)
        manages their own; another user's need users:manage, and a non-admin
        manager may not touch an admin's."""
        if not username or username == actor.username:
            return actor.username
        if auth_mod.CAP_USERS_MANAGE not in auth.effective_caps(actor):
            raise HTTPException(403, f"missing capability: {auth_mod.CAP_USERS_MANAGE}")
        target = auth_mod.get_user(client, username, prefix=prefix)
        if target is None:
            raise HTTPException(404, "no such user")
        _guard_admin_target(actor, target)
        return username

    @router.get("/tokens")
    def api_list_tokens(username: str | None = None,
                        actor: User = Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        client = _client()
        owner = _token_owner(client, actor, username)
        return {"tokens": [_token_json(t) for t in
                           auth_mod.list_api_tokens(client, owner, prefix=prefix)]}

    @router.post("/tokens")
    def api_create_token(body: CreateToken,
                         actor: User = Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        try:
            token, rec = auth_mod.create_api_token(
                _client(), actor.username, body.name, body.expires_days, prefix=prefix)
        except auth_mod.ApiTokenLimitError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**_token_json(rec), "token": token}

    @router.post("/tokens/revoke")
    def api_revoke_token(body: RevokeToken,
                         actor: User = Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        client = _client()
        owner = _token_owner(client, actor, body.username)
        if not auth_mod.revoke_api_token(client, owner, body.token_id, prefix=prefix):
            raise HTTPException(404, "no such token")
        return {"ok": True}

    @router.post("/tokens/revoke-all")
    def api_revoke_all_tokens(body: RevokeAllTokens,
                              actor: User = Depends(auth.require_cap(auth_mod.CAP_USERS_MANAGE))):
        client = _client()
        owner = _token_owner(client, actor, body.username)
        auth_mod.delete_user_api_tokens(client, owner, prefix=prefix)
        return {"ok": True}
```

Lifecycle hooks — in `api_update_user`, next to the existing `if disabled or body.password is not None:` session cleanup, add:

```python
        if disabled:
            # A disabled user's agents must stop too (#74). A password reset
            # alone does NOT revoke API tokens: they are independent credentials.
            auth_mod.delete_user_api_tokens(client, u.username, prefix=prefix)
```

In `api_delete_user`, after `auth_mod.delete_user_sessions(...)`:

```python
        auth_mod.delete_user_api_tokens(client, body.username, prefix=prefix)
```

Note: `_guard_admin_target` is defined above `/repos` in the same closure; if `_token_owner` is placed before its definition, move `_token_owner` below it. `revoke-all` on oneself by a `users:manage` holder is allowed (owner == actor).

- [ ] **Step 4: Run the tests**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tokens_api.py tests/test_api.py tests/test_auth_api.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/api.py tests/test_tokens_api.py
git commit -m "feat(api): /api/tokens create/list/revoke + drop tokens with user (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `/mcp` transport, auth wrapper, and guard

**Files:**
- Modify: `src/tpk/mcp_server.py` (`build_server`)
- Create: `src/tpk/mcp_http.py`
- Modify: `src/tpk/server.py` (`create_app`: `FastAPI(...)` construction ~line 167, and where the graph router is included ~line 519)
- Test: `tests/test_mcp_http.py` (create); `tests/test_mcp_server.py` must pass unchanged

**Interfaces:**
- Consumes: `auth.resolve_api_token`, `auth.resolve_scope`, `auth.get_user`, `AuthLayer._client()`, `AuthLayer.effective_caps(user)`, `tools.ROLE_SCOPE`, `config.setting`, `config.as_bool`.
- Produces:
  - `mcp_server.build_server(kg, guard=None)` — `guard` is `async (ctx, fn, *, tool: str, needs_source: bool = False) -> Any`; `None` = call `fn()` directly (stdio).
  - `mcp_http.enabled() -> bool`
  - `mcp_http.create_mcp_route(kg, auth, prefix: str = "") -> tuple[starlette.routing.Route, MCPServer]`
  - ASGI scope contract: the wrapper sets `scope["state"]["tpk_user"]` (`auth.User`) and `scope["state"]["tpk_token_id"]` (`str`); the guard reads them via `ctx.request_context.request.scope["state"]`.

Verified facts about the SDK (probed against the installed `mcp` 2.0): `server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))` returns a Starlette app whose own route is `/mcp`; appending `Route("/mcp", endpoint=<ASGI instance>, methods=["GET","POST","DELETE"])` to `app.router.routes` (a Route, **not** `app.mount`, so the full path is preserved and it does not collide with the `StaticFiles` mount at `/`) works; the transport needs `async with server.session_manager.run():` in the app lifespan; `ctx.request_context.request` is the Starlette `Request`, and `scope["state"]` set by an outer ASGI wrapper is visible there. `TestClient` only runs the lifespan when used as a context manager (`with TestClient(app) as c:`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_http.py`:

```python
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import requires_timeplus
from test_graph_api import SEED_EDGES, SEED_NODES, _eventually, _NoAgent, _seed_corpus_entry
from tpk import auth
from tpk.ingest import upsert_graph
from tpk.server import create_app
from tpk.tools import KnowledgeGraph

pytestmark = requires_timeplus

H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(c, token, method, params=None, id_=1):
    headers = dict(H)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", headers=headers,
                  json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def _call(c, token, tool, args):
    r = _rpc(c, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result.get("isError", False), json.dumps(result)


def _seed(tp, tmp_path):
    client, prefix = tp
    upsert_graph(client, prefix, SEED_NODES, SEED_EDGES, datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "alpha", "v1", enabled=True)
    beta = tmp_path / "beta_checkout"
    beta.mkdir()
    (beta / "b.py").write_text("b1\nb2\nb3\nb4\nb5\n")
    _seed_corpus_entry(client, prefix, "beta", "v1", enabled=True, path=beta)
    _eventually(lambda: client.query(f"SELECT count() FROM table({prefix}kg_nodes)").result_rows[0][0],
                lambda v: v == len(SEED_NODES))
    _eventually(lambda: client.query(f"SELECT count() FROM table({prefix}kg_edges)").result_rows[0][0],
                lambda v: v == len(SEED_EDGES))
    return KnowledgeGraph(client, stream_prefix=prefix, repo_paths={}, corpus_ttl=0)


@pytest.fixture()
def env(tp, tmp_path):
    client, prefix = tp
    kg = _seed(tp, tmp_path)
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1"], capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_role(client, auth.Role("beta-src", ["beta@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_SOURCE_VIEW]), prefix=prefix)
    auth.upsert_role(client, auth.Role("chatter", ["alpha@v1"], capabilities=[auth.CAP_CHAT]), prefix=prefix)
    users = {"root": auth.ROLE_ADMIN, "scoped": "alpha-only", "betasrc": "beta-src", "carl": "chatter"}
    tokens = {}
    for name, role in users.items():
        auth.upsert_user(client, auth.User(name, auth.hash_password("password-1"), role), prefix=prefix)
        tokens[name], _ = auth.create_api_token(client, name, "t", prefix=prefix)
    for name, tok in tokens.items():
        _eventually(lambda t=tok: auth.resolve_api_token(client, t, prefix=prefix))
        _eventually(lambda n=name: auth.get_user(client, n, prefix=prefix))
    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=kg)
    with TestClient(app) as c:
        yield c, client, prefix, tokens


def test_auth_matrix(env):
    c, client, prefix, tokens = env
    r = _rpc(c, None, "tools/list")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert _rpc(c, "tpk_bogus", "tools/list").status_code == 401
    # a login SESSION token is not an API token
    session = c.post("/auth/login", json={"username": "root", "password": "password-1"}).json()["token"]
    assert _rpc(c, session, "tools/list").status_code == 401
    # no `explore`
    r = _rpc(c, tokens["carl"], "tools/list")
    assert r.status_code == 403 and "explore" in r.text
    # must_change_password
    auth.upsert_user(client, auth.User("scoped", auth.hash_password("password-1"), "alpha-only",
                                       must_change_password=True), prefix=prefix)
    r = _eventually(lambda: _rpc(c, tokens["scoped"], "tools/list"), lambda r: r.status_code == 403)
    assert r.json()["detail"]["code"] == "password_change_required"
    # disabled
    auth.upsert_user(client, auth.User("betasrc", auth.hash_password("password-1"), "beta-src",
                                       disabled=True), prefix=prefix)
    assert _eventually(lambda: _rpc(c, tokens["betasrc"], "tools/list"),
                       lambda r: r.status_code == 401).status_code == 401


def test_initialize_and_lists_six_tools(env):
    c, _, _, tokens = env
    init = _rpc(c, tokens["root"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"}})
    assert init.status_code == 200 and "result" in init.json()
    names = {t["name"] for t in _rpc(c, tokens["root"], "tools/list").json()["result"]["tools"]}
    assert names == {"search_entities", "get_entity", "neighbors",
                     "path_between", "list_communities", "read_source"}


def test_admin_unscoped_and_scoped_user_isolated_per_tool(env):
    c, _, _, tokens = env
    root, scoped = tokens["root"], tokens["scoped"]

    _, out = _call(c, root, "search_entities", {"query": "Widget"})
    assert "AlphaWidget" in out and "BetaWidget" in out
    _, out = _call(c, scoped, "search_entities", {"query": "Widget"})
    assert "AlphaWidget" in out and "BetaWidget" not in out

    _, out = _call(c, root, "get_entity", {"entity_id": "gbn1"})
    assert "BetaWidget" in out
    _, out = _call(c, scoped, "get_entity", {"entity_id": "gbn1"})
    assert "BetaWidget" not in out

    _, out = _call(c, scoped, "neighbors", {"entity_id": "gan1"})
    assert "AlphaHelper" in out
    _, out = _call(c, scoped, "neighbors", {"entity_id": "gbn1"})
    assert "BetaWidget" not in out

    _, out = _call(c, scoped, "path_between", {"id_a": "gan1", "id_b": "gan2"})
    assert "gan2" in out
    _, out = _call(c, scoped, "path_between", {"id_a": "gan1", "id_b": "gbn1"})
    assert "gbn1" not in out or "null" in out

    _, out = _call(c, root, "list_communities", {})
    assert "beta@v1" in out
    _, out = _call(c, scoped, "list_communities", {})
    assert "alpha@v1" in out and "beta@v1" not in out


def test_read_source_needs_source_view_and_scope(env):
    c, _, _, tokens = env
    args = {"repo": "beta@v1", "file_path": "b.py", "line_start": 1, "line_end": 2}
    is_err, out = _call(c, tokens["root"], "read_source", args)
    assert not is_err and "b1" in out
    is_err, out = _call(c, tokens["betasrc"], "read_source", args)
    assert not is_err and "b1" in out
    # `scoped` lacks source:view -> tool error, no source
    is_err, out = _call(c, tokens["scoped"], "read_source", args)
    assert is_err and "source:view" in out and "b1" not in out


def test_role_change_applies_on_next_call(env):
    c, client, prefix, tokens = env
    _, out = _call(c, tokens["scoped"], "search_entities", {"query": "Widget"})
    assert "BetaWidget" not in out
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1", "beta@v1"],
                                       capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    out = _eventually(lambda: _call(c, tokens["scoped"], "search_entities", {"query": "Widget"})[1],
                      lambda o: "BetaWidget" in o)
    assert "BetaWidget" in out


def test_revoked_token_401(env):
    c, client, prefix, tokens = env
    tid = auth.list_api_tokens(client, "root", prefix=prefix)[0].token_id
    auth.revoke_api_token(client, "root", tid, prefix=prefix)
    assert _eventually(lambda: _rpc(c, tokens["root"], "tools/list"),
                       lambda r: r.status_code == 401).status_code == 401


def test_store_down_503(tp, tmp_path):
    client, prefix = tp
    kg = _seed(tp, tmp_path)

    class DownAuth(auth.AuthLayer):
        def _client(self):
            raise RuntimeError("store down")

    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=kg, auth=DownAuth(prefix))
    with TestClient(app) as c:
        assert _rpc(c, "tpk_anything", "tools/list").status_code == 503


def test_disabled_by_config(tp, tmp_path, monkeypatch):
    monkeypatch.setenv("TPK_MCP_HTTP_ENABLED", "0")
    client, prefix = tp
    app = create_app(agent=_NoAgent(), stream_prefix=prefix, kg=_seed(tp, tmp_path))
    with TestClient(app) as c:
        assert _rpc(c, "tpk_anything", "tools/list").status_code in (404, 405)
```

- [ ] **Step 2: Run to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_mcp_http.py -q`
Expected: FAIL (404/405 on `/mcp`).

- [ ] **Step 3: Make `build_server` guard-aware**

Replace `build_server` in `src/tpk/mcp_server.py` (keep the module docstring, the `MCPServer as FastMCP` import comment, `REPOS_TOML`, and `main()` unchanged; add `Context` to the import):

```python
from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP
```

```python
async def _unguarded(ctx, fn, *, tool: str, needs_source: bool = False):
    """stdio: local, single-user, unrestricted -- run the KG call as-is."""
    return fn()


def build_server(kg, guard=None) -> FastMCP:
    """`guard` wraps every KG call: `await guard(ctx, fn, tool=..., needs_source=...)`.
    None (stdio) runs it unrestricted; the remote HTTP endpoint passes
    mcp_http.make_guard(), which applies the caller's role scope and the
    source:view gate (#74)."""
    run = guard or _unguarded
    server = FastMCP("timeplus-knowledge")

    @server.tool()
    async def search_entities(ctx: Context, query: str, kinds: list[str] | None = None,
                              repos: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive)."""
        return await run(ctx, lambda: kg.search_entities(query, kinds=kinds, repos=repos, limit=limit),
                         tool="search_entities")

    @server.tool()
    async def get_entity(ctx: Context, entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id."""
        return await run(ctx, lambda: kg.get_entity(entity_id), tool="get_entity")

    @server.tool()
    async def neighbors(ctx: Context, entity_id: str, rels: list[str] | None = None,
                        direction: str = "both", depth: int = 1,
                        confidence: str | None = None) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED to filter edges."""
        return await run(ctx, lambda: kg.neighbors(entity_id, rels=rels, direction=direction,
                                                   depth=depth, confidence=confidence),
                         tool="neighbors")

    @server.tool()
    async def path_between(ctx: Context, id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None:
        """Shortest connection between two entities, or null if none within max_depth."""
        return await run(ctx, lambda: kg.path_between(id_a, id_b, max_depth=max_depth),
                         tool="path_between")

    @server.tool()
    async def list_communities(ctx: Context, repo: str | None = None) -> list[dict]:
        """Cluster overview: (repo, community, node_count), largest first."""
        return await run(ctx, lambda: kg.list_communities(repo=repo), tool="list_communities")

    @server.tool()
    async def read_source(ctx: Context, repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout so answers can quote real code."""
        return await run(ctx, lambda: kg.read_source(repo, file_path, line_start, line_end),
                         tool="read_source", needs_source=True)

    return server
```

Run: `uv run pytest tests/test_mcp_server.py -q` — Expected: 2 PASS (the `ctx` parameter is injected by the SDK and is not part of the tool's input schema; if `test_all_six_tools_registered` shows `ctx` leaking into a schema, the annotation is wrong — it must be exactly `ctx: Context`).

- [ ] **Step 4: Create `src/tpk/mcp_http.py`**

```python
"""Remote MCP: the knowledge-graph tools over streamable HTTP at /mcp (#74).

The caller authenticates with a per-user API token (auth.create_api_token)
and IS that tpk user: `explore` gates access, every tool call runs under the
user's role corpus scope, and read_source additionally needs `source:view`.
stdio MCP (mcp_server.main) is separate and stays unrestricted.
"""

import logging

from fastapi import HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

import tpk.auth as auth_mod
from tpk.config import as_bool, setting
from tpk.mcp_server import build_server
from tpk.tools import ROLE_SCOPE

log = logging.getLogger("tpk.mcp")

MCP_PATH = "/mcp"


def enabled() -> bool:
    return as_bool(setting("TPK_MCP_HTTP_ENABLED", "server", "mcp_http", True))


def _transport_security() -> TransportSecuritySettings:
    """The SDK's DNS-rebinding allow-list defaults to localhost and would
    reject a real hostname. /mcp is bearer-authenticated (no ambient browser
    credentials), so protection is off unless an allow-list is configured."""
    hosts = [h.strip() for h in
             str(setting("TPK_MCP_ALLOWED_HOSTS", "server", "mcp_allowed_hosts", "")).split(",")
             if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)


class McpAuth:
    """ASGI wrapper: resolve the bearer API token to a tpk user before the
    MCP transport sees the request. Status codes mirror AuthLayer (REST)."""

    def __init__(self, app, auth, prefix: str = ""):
        self.app, self.auth, self.prefix = app, auth, prefix

    def _authenticate(self, authorization: str):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ")
        if not token.startswith(auth_mod.API_TOKEN_PREFIX):
            raise HTTPException(401, "invalid or expired token")
        try:
            client = self.auth._client()
            rec = auth_mod.resolve_api_token(client, token, prefix=self.prefix)
            user = auth_mod.get_user(client, rec.username, prefix=self.prefix) if rec else None
        except Exception:
            raise HTTPException(503, "auth store unavailable")
        if user is None or user.disabled:
            raise HTTPException(401, "invalid or expired token")
        if user.must_change_password:
            raise HTTPException(403, detail={"code": "password_change_required"})
        if auth_mod.CAP_EXPLORE not in self.auth.effective_caps(user):
            raise HTTPException(403, f"missing capability: {auth_mod.CAP_EXPLORE}")
        return user, rec.token_id

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        authorization = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        try:
            user, token_id = await run_in_threadpool(self._authenticate, authorization)
        except HTTPException as e:
            headers = {"WWW-Authenticate": "Bearer"} if e.status_code == 401 else None
            return await JSONResponse({"detail": e.detail}, status_code=e.status_code,
                                      headers=headers)(scope, receive, send)
        state = scope.setdefault("state", {})
        state["tpk_user"], state["tpk_token_id"] = user, token_id
        await self.app(scope, receive, send)


def make_guard(auth, prefix: str = ""):
    """The per-tool-call guard handed to build_server. The user comes from the
    request scope set by McpAuth (NOT a ContextVar: the transport may run the
    handler in another task). Role/caps are re-read on every call so an
    admin's change applies immediately. ROLE_SCOPE is set inside the worker
    thread that runs the KG query."""

    async def guard(ctx, fn, *, tool: str, needs_source: bool = False):
        state = ctx.request_context.request.scope.get("state", {})
        user = state.get("tpk_user")
        if user is None:  # unreachable behind McpAuth; fail closed anyway
            raise PermissionError("unauthenticated")
        log.info("mcp tool call user=%s token=%s tool=%s",
                 user.username, state.get("tpk_token_id"), tool)

        def work():
            if needs_source and auth_mod.CAP_SOURCE_VIEW not in auth.effective_caps(user):
                raise PermissionError(f"missing capability: {auth_mod.CAP_SOURCE_VIEW}")
            try:
                scope = auth_mod.resolve_scope(auth._client(), user, prefix=prefix)
            except Exception:
                scope = None if user.role == auth_mod.ROLE_ADMIN else frozenset()
            token = ROLE_SCOPE.set(scope) if scope is not None else None
            try:
                return fn()
            finally:
                if token is not None:
                    ROLE_SCOPE.reset(token)

        return await run_in_threadpool(work)

    return guard


def create_mcp_route(kg, auth, prefix: str = ""):
    """(route, server): add `route` to the FastAPI app and run
    `server.session_manager.run()` in the app lifespan."""
    server = build_server(kg, guard=make_guard(auth, prefix))
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
        transport_security=_transport_security(),
    )
    route = Route(MCP_PATH, endpoint=McpAuth(inner, auth, prefix),
                  methods=["GET", "POST", "DELETE"])
    return route, server
```

- [ ] **Step 5: Wire into `create_app`**

In `src/tpk/server.py`: add `import contextlib` at the top. In `create_app`, replace `app = FastAPI(title="timeplus-knowledge")` with:

```python
    # The remote-MCP transport (#74) needs its session manager running for
    # the app's lifetime. The server is only known further down (it needs
    # `kg`), so the lifespan reads it from this holder.
    mcp_state = {"server": None}

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        if mcp_state["server"] is None:
            yield
            return
        async with mcp_state["server"].session_manager.run():
            yield

    app = FastAPI(title="timeplus-knowledge", lifespan=_lifespan)
```

Where the graph router is included (the `if kg is not None:` block around line 519 that does `app.include_router(create_graph_router(...))`), add inside the same block — this is before the `StaticFiles` mount, so `/mcp` wins over the UI catch-all:

```python
        from tpk import mcp_http

        if mcp_http.enabled():
            mcp_route, mcp_state["server"] = mcp_http.create_mcp_route(
                kg, auth, prefix=stream_prefix)
            app.router.routes.append(mcp_route)
```

(Read the surrounding lines first; if the graph router include is not inside an `if kg is not None:` guard, wrap the MCP block in one.)

- [ ] **Step 6: Run the tests**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_mcp_http.py tests/test_mcp_server.py tests/test_graph_api.py tests/test_server.py -q`
Expected: all PASS.

Known adjustment points (fix the test or code to match observed behaviour, do not weaken the security assertions):
- If a scoped `get_entity` miss renders as `null` in `structuredContent`, `"BetaWidget" not in out` already holds.
- If the SDK rejects `protocolVersion: "2025-06-18"`, use the version string from `mcp.types.LATEST_PROTOCOL_VERSION`.
- If stateless mode requires `initialize` before `tools/list` in the same request cycle, it does not (stateless = each POST is independent; verified in the probe for `tools/call`).

- [ ] **Step 7: Full backend suite + commit**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q`
Expected: all PASS (no regressions).

```bash
git add src/tpk/mcp_server.py src/tpk/mcp_http.py src/tpk/server.py tests/test_mcp_http.py
git commit -m "feat(mcp): authenticated streamable-HTTP /mcp with per-user scope (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Web UI — "API tokens" page + admin revoke

**Files:**
- Create: `web/src/Tokens.tsx`
- Modify: `web/src/Shell.tsx` (`View`, `NAV_ITEMS`, `NAV_ICON`)
- Modify: `web/src/App.tsx` (`VIEW_CAP`, `NAV_ORDER`, render switch)
- Modify: `web/src/Users.tsx` (per-user action row, next to the reset-password / disable buttons ~line 329–356)
- Modify: `web/src/app.css` (append two classes)

**Interfaces:**
- Consumes: `/api/tokens`, `/api/tokens/revoke`, `/api/tokens/revoke-all` (Task 3); `apiFetch` from `./api`; existing CSS classes `tk-btn`, `tk-btn-secondary`, `tk-modal-overlay`, `tk-modal`, `tk-modal-header`, `tk-modal-title`, `tk-modal-spacer`, `tk-modal-close`.
- Produces: `View` gains `"tokens"`; default export `Tokens()` (no props).

The markup below reuses the existing Timeplus Console classes from `Users.tsx` / `app.css` verbatim (`tk-manage-header`, `tk-manage-title`, `tk-manage-subtitle`, `tk-manage-header-spacer`, `tk-manage-error`, `tk-users-table-card`, `tk-users-table`, `tk-users-actions`, `tk-form-field`, `tk-input`, `tk-form-required`, `tk-modal-body`, `tk-modal-footer`, `tk-btn-secondary`, `tk-btn-danger`). Check how `Users.tsx` wraps its page root (the outermost `<div className=…>` of its return) and use the same wrapper class for `Tokens`; only two new classes are added (`tk-token-created`, `tk-token-value`).

- [ ] **Step 1: Add the view to the shell**

`web/src/Shell.tsx`:

```tsx
export type View = "chat" | "explorer" | "manage" | "users" | "tokens";
```

Add to `NAV_ITEMS` after the `explorer` entry (tokens are an Explorer-tier feature; both need `explore`):

```tsx
  { key: "tokens", label: "API tokens", cap: CAP.explore },
```

Add to `NAV_ICON` (a key glyph, same 24-box stroke style as the others):

```tsx
  tokens: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="M10.8 12.2 20 3m-3.5 3.5 2.5 2.5M14 9l2 2" />
    </>
  ),
```

`web/src/App.tsx`: add `tokens: CAP.explore,` to `VIEW_CAP`; set `NAV_ORDER` to `["chat", "explorer", "tokens", "manage", "users"]`; add `import Tokens from "./Tokens";`; in the render switch add a branch before the `manage` one:

```tsx
      ) : view === "tokens" ? (
        <Tokens />
```

- [ ] **Step 2: Create `web/src/Tokens.tsx`**

```tsx
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Token = {
  token_id: string; username: string; name: string; hint: string;
  created_at: string; expires_at: string | null; last_used_at: string | null;
};
type Created = Token & { token: string };

const EXPIRY_OPTIONS: { label: string; days: number | null }[] = [
  { label: "Never", days: null },
  { label: "30 days", days: 30 },
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
];

async function call(path: string, body?: unknown): Promise<any> {
  const resp = await apiFetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON body */ }
  if (!resp.ok) {
    const detail = data && typeof data === "object" ? data.detail : null;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${resp.status}`);
  }
  return data;
}

const fmt = (iso: string | null, empty: string) =>
  iso ? new Date(iso).toLocaleString() : empty;

export function mcpCommand(origin: string, token: string): string {
  return `claude mcp add --transport http timeplus-knowledge ${origin}/mcp --header "Authorization: Bearer ${token}"`;
}

export default function Tokens() {
  const [tokens, setTokens] = useState<Token[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [days, setDays] = useState<number | null>(null);
  const [created, setCreated] = useState<Created | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [copied, setCopied] = useState("");

  const refresh = useCallback(async () => {
    try { setTokens((await call("/api/tokens")).tokens); setError(""); }
    catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  async function create() {
    try {
      const body: Record<string, unknown> = { name: name.trim() };
      if (days !== null) body.expires_days = days;
      setCreated(await call("/api/tokens", body));
      setShowCreate(false); setName(""); setDays(null);
      refresh();
    } catch (e) { setError((e as Error).message); }
  }

  async function revoke(token_id: string) {
    try { await call("/api/tokens/revoke", { token_id }); setConfirming(null); refresh(); }
    catch (e) { setError((e as Error).message); }
  }

  async function copy(label: string, text: string) {
    await navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(""), 1500);
  }

  // Dismissing the panel drops the only copy of the plaintext.
  const closeCreated = () => setCreated(null);

  return (
    <div className="tk-manage">
      {error && <div className="tk-manage-error" role="alert">{error}</div>}
      <div className="tk-manage-header">
        <div>
          <div className="tk-manage-title">API tokens</div>
          <div className="tk-manage-subtitle">
            Connect a coding agent (Claude Code, Cursor) to the knowledge graph over MCP.
            A token acts as you: same corpus access, same permissions.
          </div>
        </div>
        <div className="tk-manage-header-spacer" />
        <button type="button" className="tk-btn" onClick={() => setShowCreate(true)}>New token</button>
      </div>

      {created && (
        <div className="tk-token-created" role="status">
          <strong>Copy this token now — it will not be shown again.</strong>
          <pre className="tk-token-value">{created.token}</pre>
          <button type="button" className="tk-btn tk-btn-secondary"
                  onClick={() => copy("token", created.token)}>
            {copied === "token" ? "Copied" : "Copy token"}
          </button>
          <div className="tk-manage-subtitle">Add it to Claude Code:</div>
          <pre className="tk-token-value">{mcpCommand(window.location.origin, created.token)}</pre>
          <button type="button" className="tk-btn tk-btn-secondary"
                  onClick={() => copy("cmd", mcpCommand(window.location.origin, created.token))}>
            {copied === "cmd" ? "Copied" : "Copy command"}
          </button>
          <button type="button" className="tk-btn" onClick={closeCreated}>Done</button>
        </div>
      )}

      <div className="tk-users-table-card">
      <table className="tk-users-table">
        <thead>
          <tr><th>Name</th><th>Token</th><th>Created</th><th>Expires</th><th>Last used</th><th /></tr>
        </thead>
        <tbody>
          {tokens.length === 0 && (
            <tr><td colSpan={6}>No tokens yet.</td></tr>
          )}
          {tokens.map((t) => (
            <tr key={t.token_id}>
              <td>{t.name}</td>
              <td><code>tpk_…{t.hint}</code></td>
              <td>{fmt(t.created_at, "")}</td>
              <td>{fmt(t.expires_at, "Never")}</td>
              <td>{fmt(t.last_used_at, "Never")}</td>
              <td className="tk-users-actions">
                {confirming === t.token_id ? (
                  <>
                    <button type="button" className="tk-btn tk-btn-danger" onClick={() => revoke(t.token_id)}>Confirm revoke</button>
                    <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setConfirming(null)}>Cancel</button>
                  </>
                ) : (
                  <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setConfirming(t.token_id)}>Revoke</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      {showCreate && (
        <div className="tk-modal-overlay" onClick={() => setShowCreate(false)}>
          <div className="tk-modal" role="dialog" aria-modal="true" aria-labelledby="new-token-title"
               onClick={(e) => e.stopPropagation()}>
            <div className="tk-modal-header">
              <div id="new-token-title" className="tk-modal-title">New API token</div>
              <div className="tk-modal-spacer" />
              <button type="button" className="tk-modal-close" aria-label="Close"
                      onClick={() => setShowCreate(false)}>&times;</button>
            </div>
            <form onSubmit={(e) => { e.preventDefault(); create(); }}>
              <div className="tk-modal-body">
                <div className="tk-form-field">
                  <label htmlFor="tok-name">
                    Name <span className="tk-form-required">*</span>
                  </label>
                  <input id="tok-name" className="tk-input" value={name} maxLength={64} required autoFocus
                         placeholder="e.g. laptop — Claude Code"
                         onChange={(e) => setName(e.target.value)} />
                </div>
                <div className="tk-form-field">
                  <label htmlFor="tok-expiry">Expires</label>
                  <select id="tok-expiry" className="tk-input" value={days ?? ""}
                          onChange={(e) => setDays(e.target.value ? Number(e.target.value) : null)}>
                    {EXPIRY_OPTIONS.map((o) => (
                      <option key={o.label} value={o.days ?? ""}>{o.label}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="tk-modal-footer">
                <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
                <button type="submit" className="tk-btn" disabled={!name.trim()}>Create</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
```

Append to `web/src/app.css` (tokens are the real custom properties defined at the top of that file):

```css
.tk-token-created { border: 1px solid var(--gray-700); border-radius: var(--radius); background: var(--white); padding: 16px; margin: 16px 0; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.tk-token-value { width: 100%; box-sizing: border-box; margin: 0; padding: 8px 12px; border-radius: var(--radius); background: var(--gray-900); font-size: 12px; white-space: pre-wrap; word-break: break-all; }
```

- [ ] **Step 3: Users page — revoke a user's tokens**

In `web/src/Users.tsx`, in the per-user action area (next to the reset-password / disable / delete buttons, which are rendered only when the viewer can manage users — reuse that same condition), add:

```tsx
<button type="button" className="tk-btn tk-btn-secondary"
        onClick={() => act(() => call("/api/tokens/revoke-all", { username: u.username }))}>
  Revoke API tokens
</button>
```

(`act` and `call` already exist in this file; `act` runs the promise, surfaces errors, and refreshes.)

- [ ] **Step 4: Build + gates**

Run: `cd web && npm run build && npm run check:sanitize`
Expected: `tsc -b` clean (the `Record<View, …>` maps in `Shell.tsx`/`App.tsx` force the new `tokens` key everywhere), vite build OK, sanitize check OK. If `package.json` also has `check:citations`, run it too.

- [ ] **Step 5: Visual check**

Start the stack (`make up` or `TIMEPLUS_HOST=localhost uv run tpk serve` + `cd web && npm run dev`), log in as admin, open "API tokens": create a token (copy-once panel shows the token and the `claude mcp add` command with the page origin), see it listed as `tpk_…abcd`, revoke it. Log in as a chat-only role: no "API tokens" nav item. On Users: "Revoke API tokens" empties the target's list.

- [ ] **Step 6: Commit**

```bash
git add web/src
git commit -m "feat(web): API tokens page + admin revoke for remote MCP (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Docs, config samples, end-to-end verification

**Files:**
- Modify: `README.md` ("Use from Claude Code (MCP)" section ~line 633; Configuration matrix)
- Modify: `docs/FEATURES.md` (§5 MCP integration ~line 68; the role-scope sentence ~line 97)
- Modify: `deploy/k8s/README.md`
- Modify: `.env.example` (~line 70), `repos.toml` (~line 53), `deploy/docker/repos.container.toml` (~line 47)

- [ ] **Step 1: Config samples**

`.env.example`, after the `TPK_CHAT_AUDIT` line:

```
# TPK_MCP_HTTP_ENABLED=1           # [server].mcp_http     false/0 disables the remote MCP endpoint (/mcp)
# TPK_MCP_ALLOWED_HOSTS=           # [server].mcp_allowed_hosts  comma-separated Host allow-list for /mcp (empty = no check)
```

`repos.toml` and `deploy/docker/repos.container.toml`, in the commented `[server]` block after `chat_audit` (match each file's column alignment):

```
# mcp_http     = true                # env: TPK_MCP_HTTP_ENABLED   (false/0 disables the remote MCP endpoint /mcp)
# mcp_allowed_hosts = ""             # env: TPK_MCP_ALLOWED_HOSTS  (comma-separated Host allow-list; empty = no check)
```

Add both keys to the README "Configuration" matrix in the same row format as `TPK_SESSION_TTL`.

- [ ] **Step 2: README**

Restructure "Use from Claude Code (MCP)" into two subsections. Keep the existing stdio text under **Local (stdio)** and add a closing line: "stdio MCP is local and unrestricted: no login, no role scope." Add before it:

```markdown
### Remote (HTTP) — a deployed tpk

`tpk serve` exposes the same six tools over MCP streamable HTTP at `/mcp`.
Your agent authenticates with a personal API token and acts **as you**: same
corpus scope, same permissions (`explore` to connect, `source:view` for
`read_source`). Changes an admin makes to your role apply on the next call.

1. In the web UI open **API tokens → New token**, name it, pick an expiry,
   and copy the token (shown once).
2. Register it:

       claude mcp add --transport http timeplus-knowledge https://<host>/mcp \
         --header "Authorization: Bearer tpk_…"

3. `claude mcp list` should show `timeplus-knowledge ✔ Connected`.

Tokens are valid only at `/mcp` (not the REST API). Revoke them on the same
page; disabling or deleting a user revokes theirs. Admins can revoke a
user's tokens from **Users**. Disable the endpoint with
`TPK_MCP_HTTP_ENABLED=0`; restrict accepted `Host` headers with
`TPK_MCP_ALLOWED_HOSTS`.
```

- [ ] **Step 3: FEATURES.md**

In §5, add the remote form alongside the stdio command with two sentences mirroring the README (token → `claude mcp add --transport http`; runs as the token's user). Replace the phrase "(admin, MCP, and CLI are unrestricted)" with "(admin, the local stdio MCP server, and the CLI are unrestricted; the remote `/mcp` endpoint runs as the token's user and is scoped like chat)". Add a row to the commands/surfaces table: `` `/mcp` `` — "Remote MCP endpoint (streamable HTTP, per-user API token)".

- [ ] **Step 4: k8s README**

Add a section "Connect a coding agent (MCP)": `/mcp` is served by the app container on the same port/Service as the UI — no manifest change in any of the three modes; TLS terminates where it already does (NLB + ACM in the app-only reference deployment); then the 3-step token flow with `https://<your-host>/mcp`. Verify by grepping the manifests that nothing path-filters requests (`grep -rn "path" deploy/k8s/*.yaml`); if an Ingress with path rules exists, document adding `/mcp`.

- [ ] **Step 5: End-to-end manual verification**

```bash
TIMEPLUS_HOST=localhost uv run tpk serve   # note the port it prints
```

Log in to the UI, create a token, then:

```bash
claude mcp add --transport http tpk-remote-test http://localhost:<port>/mcp --header "Authorization: Bearer tpk_…"
claude mcp list        # expect: tpk-remote-test … ✔ Connected
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:<port>/mcp   # expect 401
claude mcp remove tpk-remote-test
```

Record the observed output in the PR description.

- [ ] **Step 6: Full verification + commit**

Run: `TIMEPLUS_HOST=localhost uv run pytest -q && (cd web && npm run build && npm run check:sanitize)`
Expected: all PASS.

```bash
git add README.md docs/FEATURES.md deploy/k8s/README.md .env.example repos.toml deploy/docker/repos.container.toml
git commit -m "docs: remote MCP setup, API tokens, new [server] config keys (#74)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 7: Open the PR**

```bash
git push -u origin feat/remote-mcp
gh pr create --title "Remote MCP: authenticated streamable-HTTP /mcp + per-user API tokens (#74)" --body "<summary, test evidence, manual claude mcp list output; Closes #74>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

The user reviews before merge (project PR workflow).

---

## Spec coverage check

| Spec section | Task |
|---|---|
| §1 Transport (mount, stateless, lifespan, DNS-rebinding, enable switch, no-kg → not mounted) | 4 |
| §2 Token store (columns, format, helpers, throttle, cap, lifecycle) | 1 (store), 3 (lifecycle hooks) |
| §3 Wrapper status matrix + guard (scope, source:view, in-thread ContextVar, logging, shared `resolve_scope`) | 2, 4 |
| §4 REST API | 3 |
| §5 Web UI | 5 |
| §6 Audit (log line + `last_used_at`) | 1, 4 |
| §7 Deploy & docs | 6 |
| §8 Testing | 1–6 |

Deviation from the spec, deliberate: `expires_at` / `last_used_at` are stored as an epoch sentinel rather than nullable columns (the schema has no nullable columns on either backend); the API still returns `null`.
