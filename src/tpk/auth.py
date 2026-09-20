"""Users, roles, and sessions: the auth store (issue #8).

`admin` is a reserved role name, never a kg_roles row: `user.role ==
ROLE_ADMIN` short-circuits to full access everywhere.
"""

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from tpk import db

log = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
SEED_USERNAME = "admin"
SEED_PASSWORD = "changeme"

# -- capabilities ----------------------------------------------------------
# A role grants a set of function capabilities, enforced identically on the
# API and the UI. `admin` (the reserved super-role) implicitly holds all of
# them. `:manage` implies `:view` (see expand_capabilities).
CAP_CHAT = "chat"
CAP_EXPLORE = "explore"
CAP_CORPUS_VIEW = "corpus:view"
CAP_CORPUS_MANAGE = "corpus:manage"
CAP_USERS_VIEW = "users:view"
CAP_USERS_MANAGE = "users:manage"
CAP_SOURCE_VIEW = "source:view"

# Order is the canonical UI/display order.
ALL_CAPABILITIES = [
    CAP_CHAT, CAP_EXPLORE,
    CAP_CORPUS_VIEW, CAP_CORPUS_MANAGE,
    CAP_USERS_VIEW, CAP_USERS_MANAGE,
    CAP_SOURCE_VIEW,
]
_CAP_SET = frozenset(ALL_CAPABILITIES)
# `:manage` grants its `:view` sibling for free.
_MANAGE_IMPLIES_VIEW = {
    CAP_CORPUS_MANAGE: CAP_CORPUS_VIEW,
    CAP_USERS_MANAGE: CAP_USERS_VIEW,
}

# Existing roles created before capabilities shipped had no capability set;
# they migrate to chat-only (a role's stored capabilities column is the
# empty string for those rows — see Role parsing below).
DEFAULT_CAPABILITIES = [CAP_CHAT]


def expand_capabilities(caps) -> set[str]:
    """Normalize a capability collection: drop unknowns, add each `:manage`'s
    implied `:view`."""
    out: set[str] = set()
    for c in caps:
        if c in _CAP_SET:
            out.add(c)
            implied = _MANAGE_IMPLIES_VIEW.get(c)
            if implied:
                out.add(implied)
    return out


_hasher = PasswordHasher()  # argon2id defaults

_USER_COLUMNS = ["username", "password_hash", "role", "must_change_password",
                 "disabled", "daily_token_limit", "created_at", "updated_at"]
_ROLE_COLUMNS = ["name", "entry_keys", "capabilities", "description",
                 "daily_token_limit", "updated_at"]
_SESSION_COLUMNS = ["token_hash", "username", "expires_at", "created_at"]


@dataclass
class User:
    username: str
    password_hash: str
    role: str
    must_change_password: bool = False
    disabled: bool = False
    # Per-user daily token budget override (0 = inherit the role/global limit).
    # Takes precedence over the role's daily_token_limit when > 0. See #62.
    daily_token_limit: int = 0


@dataclass
class Role:
    name: str
    entry_keys: list[str] = field(default_factory=list)
    description: str = ""
    capabilities: list[str] = field(default_factory=lambda: list(DEFAULT_CAPABILITIES))
    # Daily per-user token budget for members of this role (0 = inherit the
    # global fallback, config.daily_token_limit; NOT unlimited unless the global
    # itself is 0). A per-user override still wins over this. See #62.
    daily_token_limit: int = 0


def _parse_capabilities(raw: str) -> list[str]:
    """Decode a stored kg_roles.capabilities cell.

    An empty cell means the row predates capabilities -> it migrates to the
    chat-only default (the decision for existing roles on upgrade). A stored
    `[]` is an explicit empty grant and stays empty. Anything else is the
    JSON list the admin saved.
    """
    if raw is None or raw == "":
        return list(DEFAULT_CAPABILITIES)
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return list(DEFAULT_CAPABILITIES)
    return [c for c in val if isinstance(c, str)] if isinstance(val, list) else []


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
        db.qualified("kg_users", prefix),
        [[user.username, user.password_hash, user.role,
          user.must_change_password, user.disabled,
          max(int(user.daily_token_limit), 0), _now(), _now()]],
        column_names=_USER_COLUMNS,
    )


def get_user(client, username: str, prefix: str = "") -> User | None:
    rows = client.query(
        f"SELECT username, password_hash, role, must_change_password, disabled, daily_token_limit"
        f" FROM {db.latest(db.qualified('kg_users', prefix))} WHERE username = %(u)s",
        parameters={"u": username},
    ).result_rows
    if not rows:
        return None
    u, h, r, mc, dis, lim = rows[0]
    return User(u, h, r, bool(mc), bool(dis), daily_token_limit=int(lim or 0))


def list_users(client, prefix: str = "") -> list[User]:
    rows = client.query(
        f"SELECT username, password_hash, role, must_change_password, disabled, daily_token_limit"
        f" FROM {db.latest(db.qualified('kg_users', prefix))} ORDER BY username"
    ).result_rows
    return [User(u, h, r, bool(mc), bool(dis), daily_token_limit=int(lim or 0))
            for u, h, r, mc, dis, lim in rows]


def delete_user(client, username: str, prefix: str = "") -> None:
    db.delete(client, db.qualified("kg_users", prefix), "username = %(u)s",
              {"u": username}, ("username",))


def admin_count(client, prefix: str = "") -> int:
    rows = client.query(
        f"SELECT count() FROM {db.latest(db.qualified('kg_users', prefix))}"
        f" WHERE role = %(r)s AND NOT disabled",
        parameters={"r": ROLE_ADMIN},
    ).result_rows
    return int(rows[0][0]) if rows else 0


def seed_admin(client, prefix: str = "") -> bool:
    if list_users(client, prefix=prefix):
        return False
    # The seeded admin is a NEW account that happens to reuse a username, so
    # any credential still keyed to `admin` (e.g. left by the break-glass
    # reset, which clears kg_users) must not authenticate as it.
    delete_user_api_tokens(client, SEED_USERNAME, prefix=prefix)
    delete_user_sessions(client, SEED_USERNAME, prefix=prefix)
    upsert_user(
        client,
        User(SEED_USERNAME, hash_password(SEED_PASSWORD), ROLE_ADMIN,
             must_change_password=True),
        prefix=prefix,
    )
    return True


def reset_admin(client, prefix: str = "", revoke_all: bool = False) -> None:
    """Break-glass recovery (#78, `tpk auth reset-admin`): put the `admin`
    account back to SEED_PASSWORD -- enabled, admin role, forced password
    change -- whatever state it is in (disabled, lost password, row deleted).
    Every other user and all roles are left alone.

    Credentials go FIRST: sessions and API tokens are keyed to a username, so
    anything still keyed to `admin` would authenticate as the recovered
    account. `revoke_all` clears EVERY user's sessions and API tokens instead
    (suspected compromise) -- accounts survive, their credentials don't. Goes
    through db.delete, so it works on both backends (proton has no DELETE)."""
    if revoke_all:
        for stream in ("kg_api_tokens", "kg_api_token_usage", "kg_sessions"):
            db.delete(client, db.qualified(stream, prefix), "1 = 1", {}, ("token_hash",))
    else:
        delete_user_api_tokens(client, SEED_USERNAME, prefix=prefix)
        delete_user_sessions(client, SEED_USERNAME, prefix=prefix)
    upsert_user(
        client,
        User(SEED_USERNAME, hash_password(SEED_PASSWORD), ROLE_ADMIN,
             must_change_password=True, disabled=False),
        prefix=prefix,
    )


# -- roles -----------------------------------------------------------------

def upsert_role(client, role: Role, prefix: str = "") -> None:
    client.insert(
        db.qualified("kg_roles", prefix),
        [[role.name, json.dumps(role.entry_keys), json.dumps(role.capabilities),
          role.description, max(int(role.daily_token_limit), 0), _now()]],
        column_names=_ROLE_COLUMNS,
    )


def get_role(client, name: str, prefix: str = "") -> Role | None:
    rows = client.query(
        f"SELECT name, entry_keys, capabilities, description, daily_token_limit"
        f" FROM {db.latest(db.qualified('kg_roles', prefix))} WHERE name = %(n)s",
        parameters={"n": name},
    ).result_rows
    if not rows:
        return None
    n, keys, caps, desc, limit = rows[0]
    return Role(n, json.loads(keys) if keys else [], desc, _parse_capabilities(caps),
                daily_token_limit=int(limit or 0))


def list_roles(client, prefix: str = "") -> list[Role]:
    rows = client.query(
        f"SELECT name, entry_keys, capabilities, description, daily_token_limit"
        f" FROM {db.latest(db.qualified('kg_roles', prefix))} ORDER BY name"
    ).result_rows
    return [Role(n, json.loads(k) if k else [], d, _parse_capabilities(c),
                 daily_token_limit=int(lim or 0))
            for n, k, c, d, lim in rows]


def delete_role(client, name: str, prefix: str = "") -> None:
    db.delete(client, db.qualified("kg_roles", prefix), "name = %(n)s", {"n": name}, ("name",))


def usernames_with_role(client, name: str, prefix: str = "") -> list[str]:
    rows = client.query(
        f"SELECT username FROM {db.latest(db.qualified('kg_users', prefix))} WHERE role = %(r)s"
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
        db.qualified("kg_sessions", prefix),
        [[_token_hash(token), username,
          _now() + timedelta(seconds=ttl_seconds), _now()]],
        column_names=_SESSION_COLUMNS,
    )
    return token


def get_session(client, token: str, prefix: str = "") -> str | None:
    h = _token_hash(token)
    rows = client.query(
        f"SELECT username, expires_at FROM {db.latest(db.qualified('kg_sessions', prefix))}"
        f" WHERE token_hash = %(h)s",
        parameters={"h": h},
    ).result_rows
    if not rows:
        return None
    username, expires_at = rows[0]
    if expires_at.replace(tzinfo=timezone.utc) < _now():
        db.delete(client, db.qualified("kg_sessions", prefix), "token_hash = %(h)s",
                  {"h": h}, ("token_hash",))
        return None
    return username


def delete_session(client, token: str, prefix: str = "") -> None:
    db.delete(client, db.qualified("kg_sessions", prefix), "token_hash = %(h)s",
              {"h": _token_hash(token)}, ("token_hash",))


def delete_user_sessions(client, username: str, prefix: str = "",
                         keep_token: str | None = None) -> None:
    where = "username = %(u)s"
    params = {"u": username}
    if keep_token is not None:
        where += " AND token_hash != %(k)s"
        params["k"] = _token_hash(keep_token)
    db.delete(client, db.qualified("kg_sessions", prefix), where, params, ("token_hash",))


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
# Last use is tracked in its own keyed stream: the credential row is never
# rewritten after creation, so a throttled touch can't undo a revoke that
# landed between its read and its write (its kg_api_tokens.last_used_at cell
# keeps the creation-time sentinel). A usage row alone authenticates nothing.
_API_TOKEN_USAGE_COLUMNS = ["token_hash", "last_used_at"]


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


def _last_used(client, hashes: list[str], prefix: str) -> dict[str, datetime]:
    """token_hash -> last use, for the hashes that have been used at all."""
    if not hashes:
        return {}
    rows = client.query(
        f"SELECT token_hash, last_used_at FROM "
        f"{db.latest(db.qualified('kg_api_token_usage', prefix))}"
        f" WHERE token_hash IN %(h)s",
        parameters={"h": hashes},
    ).result_rows
    used = ((h, _from_db(t)) for h, t in rows)
    return {h: t for h, t in used if t is not None}


def _forget_usage(client, hashes: list[str], prefix: str) -> None:
    """Drop usage rows for revoked/expired tokens. Best effort: a leftover row
    grants nothing, so it must never fail the revoke that precedes it."""
    if not hashes:
        return
    try:
        db.delete(client, db.qualified("kg_api_token_usage", prefix),
                  "token_hash IN %(h)s", {"h": hashes}, ("token_hash",))
    except Exception:
        log.warning("could not clear API token usage rows", exc_info=True)


def list_api_tokens(client, username: str, prefix: str = "") -> list[ApiToken]:
    rows = _api_token_rows(client, "username = %(u)s", {"u": username}, prefix)
    live = [(row[0], _row_to_api_token(row)) for row in rows]  # (token_hash, token)
    live = [(h, t) for h, t in live if _live(t)]
    used = _last_used(client, [h for h, _ in live], prefix)
    for h, t in live:
        # Fall back to the row's own cell for tokens last used before the
        # usage stream existed (the column is no longer written).
        t.last_used_at = used.get(h, t.last_used_at)
    return [t for _, t in live]


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
    if not _live(rec):
        db.delete(client, db.qualified("kg_api_tokens", prefix),
                  "token_hash = %(h)s", {"h": h}, ("token_hash",))
        _forget_usage(client, [h], prefix)
        return None
    now = _now()
    rec.last_used_at = _last_used(client, [h], prefix).get(h, rec.last_used_at)
    # Throttled: a busy agent must not rewrite the usage row on every call.
    # The credential row itself is never touched here -- see the module notes
    # on _API_TOKEN_USAGE_COLUMNS.
    if rec.last_used_at is None or now - rec.last_used_at > _TOUCH_INTERVAL:
        client.insert(db.qualified("kg_api_token_usage", prefix), [[h, now]],
                      column_names=_API_TOKEN_USAGE_COLUMNS)
        rec.last_used_at = now
    return rec


def revoke_api_token(client, username: str, token_id: str, prefix: str = "") -> bool:
    params = {"u": username, "i": token_id}
    where = "username = %(u)s AND token_id = %(i)s"
    rows = _api_token_rows(client, where, params, prefix)
    if not rows:
        return False
    db.delete(client, db.qualified("kg_api_tokens", prefix), where, params, ("token_hash",))
    _forget_usage(client, [r[0] for r in rows], prefix)
    return True


def delete_user_api_tokens(client, username: str, prefix: str = "") -> None:
    rows = _api_token_rows(client, "username = %(u)s", {"u": username}, prefix)
    db.delete(client, db.qualified("kg_api_tokens", prefix),
              "username = %(u)s", {"u": username}, ("token_hash",))
    _forget_usage(client, [r[0] for r in rows], prefix)


# -- HTTP layer ------------------------------------------------------------

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from tpk.config import setting


def _session_ttl() -> int:
    return setting("TPK_SESSION_TTL", "server", "session_ttl", 86400, cast=int)


def effective_capabilities(user: User, role: Role | None) -> set[str]:
    """The capabilities a user actually holds. `admin` gets all of them; any
    other user gets the (view-expanded) capabilities of their role, or the
    empty set if the role is missing/unreadable (fail closed)."""
    if user.role == ROLE_ADMIN:
        return set(ALL_CAPABILITIES)
    if role is None:
        return set()
    return expand_capabilities(role.capabilities)


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


# Precomputed at import time so an unknown-username login still pays the
# same argon2 cost as a known-user/wrong-password login -- otherwise the
# `user is None` short-circuit is a timing oracle for username enumeration
# even though both paths return the identical 401 body.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


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

    def effective_caps(self, user: User) -> set[str]:
        """Resolve a user's effective capabilities, reading their role from
        the store for non-admins. Fails closed (empty set) if the role is
        unreadable."""
        if user.role == ROLE_ADMIN:
            return set(ALL_CAPABILITIES)
        try:
            role = get_role(self._client(), user.role, prefix=self.prefix)
        except Exception:
            role = None
        return effective_capabilities(user, role)

    def require_cap(self, capability: str):
        """Build a FastAPI dependency that admits a user only if they hold
        `capability`. Usage: `Depends(auth.require_cap(auth.CAP_CHAT))`."""
        def dependency(authorization: str | None = Header(None)) -> User:
            user = self.require_user(authorization)
            if capability not in self.effective_caps(user):
                raise HTTPException(403, f"missing capability: {capability}")
            return user
        return dependency


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
        # Always run an argon2 verify, even for an unknown username, so the
        # two paths cost the same wall-clock time (see _DUMMY_HASH above).
        password_hash = user.password_hash if user is not None else _DUMMY_HASH
        password_ok = verify_password(password_hash, body.password)
        if user is None or user.disabled or not password_ok:
            raise HTTPException(401, "invalid credentials")
        try:
            token = create_session(client, user.username, _session_ttl(), prefix=prefix)
        except Exception:
            raise HTTPException(503, "auth store unavailable")
        return {"token": token, "role": user.role,
                "must_change_password": user.must_change_password,
                "capabilities": sorted(auth_layer.effective_caps(user))}

    @router.post("/logout", status_code=204)
    def logout(authorization: str | None = Header(None),
               user: User = Depends(auth_layer.require_user_any)):
        try:
            delete_session(auth_layer._client(),
                           authorization.removeprefix("Bearer "), prefix=prefix)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "auth store unavailable")

    @router.get("/me")
    def me(user: User = Depends(auth_layer.require_user_any)):
        return {"username": user.username, "role": user.role,
                "must_change_password": user.must_change_password,
                "capabilities": sorted(auth_layer.effective_caps(user))}

    @router.post("/change-password")
    def change_password(body: ChangePasswordRequest,
                        authorization: str | None = Header(None),
                        user: User = Depends(auth_layer.require_user_any)):
        if not verify_password(user.password_hash, body.old_password):
            raise HTTPException(401, "invalid credentials")
        err = validate_new_password(body.new_password, old=body.old_password)
        if err:
            raise HTTPException(400, err)
        try:
            client = auth_layer._client()
            upsert_user(client, User(user.username, hash_password(body.new_password),
                                     user.role, must_change_password=False,
                                     disabled=user.disabled), prefix=prefix)
            delete_user_sessions(client, user.username, prefix=prefix,
                                 keep_token=authorization.removeprefix("Bearer "))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "auth store unavailable")
        return {"ok": True}

    return router
