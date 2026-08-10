import pytest

from conftest import requires_timeplus

pytestmark = requires_timeplus


def test_schema_created_and_idempotent(tp):
    from tpk import db

    client, prefix = tp
    # calling again must not raise (IF NOT EXISTS)
    db.ensure_schema(client, prefix)
    names = {r[0] for r in client.query("SHOW STREAMS").result_rows}
    assert {f"{prefix}kg_nodes", f"{prefix}kg_edges", f"{prefix}kg_ingest_log"} <= names


def test_drop_schema_refuses_empty_prefix(tp):
    from tpk import db

    client, _ = tp
    with pytest.raises(ValueError):
        db.drop_schema(client, "")
