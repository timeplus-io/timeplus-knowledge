import time

from conftest import requires_timeplus
from tpk import auth

pytestmark = requires_timeplus


def _eventually(fn, predicate, timeout=5.0, interval=0.1):
    """Poll fn() until predicate(result) holds, or return the last value on
    timeout. Mutable-stream writes have a small (~100-300ms) propagation lag
    before being visible to a fresh `table(...)` read; see tests/test_ingest.py.
    """
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


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


def test_user_crud(tp):
    client, prefix = tp
    u = auth.User("alice", auth.hash_password("password-1"), role="support")
    auth.upsert_user(client, u, prefix=prefix)
    _eventually(
        lambda: auth.get_user(client, "alice", prefix=prefix),
        lambda v: v is not None,
    )
    got = auth.get_user(client, "alice", prefix=prefix)
    assert got.role == "support" and not got.disabled and not got.must_change_password
    auth.delete_user(client, "alice", prefix=prefix)
    _eventually(
        lambda: auth.get_user(client, "alice", prefix=prefix),
        lambda v: v is None,
    )


def test_role_crud_and_usernames_with_role(tp):
    client, prefix = tp
    auth.upsert_role(client, auth.Role("support", ["docs@main", "helm-charts@v13"]), prefix=prefix)
    _eventually(
        lambda: auth.get_role(client, "support", prefix=prefix),
        lambda v: v is not None,
    )
    assert auth.get_role(client, "support", prefix=prefix).entry_keys == ["docs@main", "helm-charts@v13"]
    auth.upsert_user(client, auth.User("bob", "x", role="support"), prefix=prefix)
    _eventually(
        lambda: auth.usernames_with_role(client, "support", prefix=prefix),
        lambda v: v == ["bob"],
    )


def test_sessions_create_get_expire_delete(tp):
    client, prefix = tp
    auth.upsert_user(client, auth.User("carol", "x", role="support"), prefix=prefix)
    token = auth.create_session(client, "carol", ttl_seconds=3600, prefix=prefix)
    _eventually(
        lambda: auth.get_session(client, token, prefix=prefix),
        lambda v: v == "carol",
    )
    assert auth.get_session(client, "no-such-token", prefix=prefix) is None
    expired = auth.create_session(client, "carol", ttl_seconds=-1, prefix=prefix)
    _eventually(
        lambda: auth.get_session(client, expired, prefix=prefix),
        lambda v: v is None,
    )  # lazy delete
    auth.delete_session(client, token, prefix=prefix)
    _eventually(
        lambda: auth.get_session(client, token, prefix=prefix),
        lambda v: v is None,
    )


def test_delete_user_sessions_keep_current(tp):
    client, prefix = tp
    t1 = auth.create_session(client, "dave", 3600, prefix=prefix)
    t2 = auth.create_session(client, "dave", 3600, prefix=prefix)
    _eventually(
        lambda: auth.get_session(client, t2, prefix=prefix),
        lambda v: v == "dave",
    )
    auth.delete_user_sessions(client, "dave", prefix=prefix, keep_token=t1)
    _eventually(
        lambda: auth.get_session(client, t2, prefix=prefix),
        lambda v: v is None,
    )
    assert auth.get_session(client, t1, prefix=prefix) == "dave"


def test_seed_admin_idempotent(tp):
    client, prefix = tp
    assert auth.seed_admin(client, prefix=prefix) is True
    _eventually(
        lambda: auth.get_user(client, "admin", prefix=prefix),
        lambda v: v is not None,
    )
    admin = auth.get_user(client, "admin", prefix=prefix)
    assert admin.role == auth.ROLE_ADMIN and admin.must_change_password
    assert auth.verify_password(admin.password_hash, auth.SEED_PASSWORD)
    assert auth.seed_admin(client, prefix=prefix) is False  # never re-seed
    assert auth.admin_count(client, prefix=prefix) == 1
