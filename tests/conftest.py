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
