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


# -- HTTP layer ------------------------------------------------------------

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel


def _session_ttl() -> int:
    return int(os.environ.get("TPK_SESSION_TTL", "86400"))


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
                "must_change_password": user.must_change_password}

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
