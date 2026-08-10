from datetime import datetime, timezone

from tpk.model import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    Edge,
    Node,
    edge_rows,
    node_id,
    node_rows,
)


def test_node_id_is_deterministic_and_distinct():
    a = node_id("proton", "function", "Checkpoint::flush")
    b = node_id("proton", "function", "Checkpoint::flush")
    c = node_id("proton", "class", "Checkpoint::flush")
    assert a == b
    assert a != c
    assert len(a) == 16


def _node(**over):
    base = dict(
        id="abc", repo="docs", kind="doc_section", name="Install",
        qualified_name="install/index.md#Install", file_path="install/index.md",
        line_start=1, line_end=40, summary="How to install",
        community="c1", visibility="public",
    )
    base.update(over)
    return Node(**base)


def test_node_rows_match_column_order():
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    rows = node_rows([_node()], ts)
    assert len(rows) == 1
    assert len(rows[0]) == len(NODE_COLUMNS)
    assert rows[0][NODE_COLUMNS.index("id")] == "abc"
    assert rows[0][-1] == ts and NODE_COLUMNS[-1] == "updated_at"


def test_edge_rows_match_column_order():
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    e = Edge(src="abc", dst="def", rel="documents", confidence="EXTRACTED", repo="docs")
    rows = edge_rows([e], ts)
    assert rows[0][EDGE_COLUMNS.index("rel")] == "documents"
    assert rows[0][-1] == ts and EDGE_COLUMNS[-1] == "updated_at"
