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
