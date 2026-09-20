"""Break-glass admin recovery (#78): `tpk auth reset-admin`."""
import time

import pytest
from typer.testing import CliRunner

from conftest import requires_timeplus
from tpk import auth
from tpk import cli as cli_mod
from tpk.cli import app

pytestmark = requires_timeplus


def _eventually(fn, predicate=bool, timeout=5.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


@pytest.fixture()
def locked_out(tp):
    """Every admin is unusable (disabled, unknown password) and still holds a
    session and an API token; a regular user `bob` has credentials too."""
    client, prefix = tp
    auth.upsert_role(client, auth.Role("support", ["docs@main"]), prefix=prefix)
    auth.upsert_user(client, auth.User("admin", auth.hash_password("lost-password-1"),
                                       auth.ROLE_ADMIN, disabled=True), prefix=prefix)
    auth.upsert_user(client, auth.User("bob", auth.hash_password("password-1"), "support"),
                     prefix=prefix)
    creds = {
        "admin_session": auth.create_session(client, "admin", 3600, prefix=prefix),
        "admin_token": auth.create_api_token(client, "admin", "laptop", prefix=prefix)[0],
        "bob_session": auth.create_session(client, "bob", 3600, prefix=prefix),
        "bob_token": auth.create_api_token(client, "bob", "laptop", prefix=prefix)[0],
    }
    _eventually(lambda: auth.resolve_api_token(client, creds["bob_token"], prefix=prefix))
    _eventually(lambda: auth.resolve_api_token(client, creds["admin_token"], prefix=prefix))
    _eventually(lambda: auth.get_session(client, creds["bob_session"], prefix=prefix))
    _eventually(lambda: auth.get_session(client, creds["admin_session"], prefix=prefix))
    return client, prefix, creds


def _admin_is_recovered(client, prefix):
    u = auth.get_user(client, "admin", prefix=prefix)
    return bool(u and not u.disabled and u.must_change_password
                and u.role == auth.ROLE_ADMIN
                and auth.verify_password(u.password_hash, auth.SEED_PASSWORD))


def test_reset_admin_recovers_admin_and_kills_only_admin_credentials(locked_out):
    client, prefix, creds = locked_out
    auth.reset_admin(client, prefix=prefix)
    assert _eventually(lambda: _admin_is_recovered(client, prefix))
    # the old admin's credentials must not authenticate as the recovered account
    assert _eventually(lambda: auth.resolve_api_token(client, creds["admin_token"], prefix=prefix),
                       lambda v: v is None) is None
    assert _eventually(lambda: auth.get_session(client, creds["admin_session"], prefix=prefix),
                       lambda v: v is None) is None
    # everyone else is untouched
    assert auth.get_user(client, "bob", prefix=prefix).role == "support"
    assert auth.resolve_api_token(client, creds["bob_token"], prefix=prefix) is not None
    assert auth.get_session(client, creds["bob_session"], prefix=prefix) == "bob"
    assert auth.get_role(client, "support", prefix=prefix) is not None


def test_reset_admin_revoke_all_kills_every_credential_but_keeps_users(locked_out):
    client, prefix, creds = locked_out
    auth.reset_admin(client, prefix=prefix, revoke_all=True)
    assert _eventually(lambda: _admin_is_recovered(client, prefix))
    assert _eventually(lambda: auth.resolve_api_token(client, creds["bob_token"], prefix=prefix),
                       lambda v: v is None) is None
    assert _eventually(lambda: auth.get_session(client, creds["bob_session"], prefix=prefix),
                       lambda v: v is None) is None
    assert auth.list_api_tokens(client, "bob", prefix=prefix) == []
    # accounts and roles survive: only credentials are revoked
    assert auth.get_user(client, "bob", prefix=prefix) is not None
    assert auth.get_role(client, "support", prefix=prefix) is not None


def test_reset_admin_creates_admin_when_the_row_is_gone(tp):
    client, prefix = tp
    auth.upsert_user(client, auth.User("bob", auth.hash_password("password-1"), "support"),
                     prefix=prefix)   # store not empty -> seed_admin would do nothing
    _eventually(lambda: auth.get_user(client, "bob", prefix=prefix))
    auth.reset_admin(client, prefix=prefix)
    assert _eventually(lambda: _admin_is_recovered(client, prefix))


def _cli(monkeypatch, client, prefix):
    monkeypatch.setattr(cli_mod.db, "get_client", lambda settings: client)
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)
    return CliRunner()


def test_cli_reset_admin_declined_changes_nothing(locked_out, monkeypatch):
    client, prefix, creds = locked_out
    result = _cli(monkeypatch, client, prefix).invoke(app, ["auth", "reset-admin"], input="n\n")
    assert result.exit_code != 0
    time.sleep(0.5)
    assert auth.get_user(client, "admin", prefix=prefix).disabled is True
    assert auth.resolve_api_token(client, creds["admin_token"], prefix=prefix) is not None


def test_cli_reset_admin_yes(locked_out, monkeypatch):
    client, prefix, creds = locked_out
    result = _cli(monkeypatch, client, prefix).invoke(app, ["auth", "reset-admin", "--yes"])
    assert result.exit_code == 0, result.output
    assert "admin" in result.output and auth.SEED_PASSWORD in result.output
    assert "change" in result.output.lower()          # tells the operator to change it
    assert _eventually(lambda: _admin_is_recovered(client, prefix))
    assert auth.resolve_api_token(client, creds["bob_token"], prefix=prefix) is not None


def test_cli_reset_admin_revoke_all(locked_out, monkeypatch):
    client, prefix, creds = locked_out
    result = _cli(monkeypatch, client, prefix).invoke(
        app, ["auth", "reset-admin", "--revoke-all", "--yes"])
    assert result.exit_code == 0, result.output
    assert _eventually(lambda: auth.resolve_api_token(client, creds["bob_token"], prefix=prefix),
                       lambda v: v is None) is None
