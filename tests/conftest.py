import os
import uuid

import pytest

requires_timeplus = pytest.mark.skipif(
    not os.environ.get("TIMEPLUS_HOST"),
    reason="TIMEPLUS_HOST not set; integration tests need a running timeplusd",
)


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
