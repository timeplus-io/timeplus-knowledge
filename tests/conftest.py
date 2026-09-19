import os
import uuid

import pytest

requires_timeplus = pytest.mark.skipif(
    not os.environ.get("TIMEPLUS_HOST"),
    reason="TIMEPLUS_HOST not set; integration tests need a running timeplusd",
)


@pytest.fixture(autouse=True)
def _disable_chat_audit_by_default(monkeypatch):
    """The /chat handler auto-builds a Timeplus audit sink unless disabled,
    which would attempt a real DB connect on every chat turn. Unit tests are
    infra-free, so default it off; tests exercising auditing inject their own
    sink via create_app(audit_sink=...), which takes precedence over this."""
    monkeypatch.setenv("TPK_CHAT_AUDIT", "0")


@pytest.fixture()
def tp():
    from tpk.config import Settings
    from tpk import db

    settings = Settings.from_env()
    prefix = f"test_{uuid.uuid4().hex[:6]}_"
    client = db.get_client(settings)
    db.ensure_schema(client, prefix)
    yield client, prefix
    db.drop_schema(client, prefix)


# Backend-parametrized integration fixture (#50). Each backend is exercised
# against its own Timeplus instance, addressed by an env var so the two can be
# distinct servers (timeplusd Enterprise vs OSS proton):
#   TPK_TEST_TIMEPLUSD_PORT / TPK_TEST_PROTON_PORT  (host localhost, user default)
# A backend whose port env is unset is skipped, mirroring `requires_timeplus`.
_BACKEND_ENV = {
    "timeplusd": "TPK_TEST_TIMEPLUSD_PORT",
    "proton": "TPK_TEST_PROTON_PORT",
}

_KG_STREAMS = (
    "kg_nodes", "kg_edges", "kg_ingest_log", "kg_repos", "kg_users",
    "kg_roles", "kg_sessions", "kg_api_tokens", "chat_audit_log",
)


@pytest.fixture(params=["timeplusd", "proton"])
def backend_tp(request, monkeypatch):
    backend = request.param
    port = os.environ.get(_BACKEND_ENV[backend])
    if not port:
        pytest.skip(f"{_BACKEND_ENV[backend]} not set; no {backend} instance to test")
    monkeypatch.setenv("TPK_DB_BACKEND", backend)
    from tpk.config import Settings
    from tpk import db

    host = os.environ.get("TPK_TEST_HOST", "localhost")
    client = db.get_client(Settings(host=host, user="default", password="", port=int(port)))
    prefix = f"test_{uuid.uuid4().hex[:6]}_"
    db.ensure_schema(client, prefix)
    yield client, prefix, backend
    for name in _KG_STREAMS:
        client.command(f"DROP STREAM IF EXISTS {prefix}{name}")
