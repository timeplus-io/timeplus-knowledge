"""Token metering + daily-budget accounting (issue #62)."""

from datetime import datetime, timezone

from tpk import usage


class _FakeClient:
    def __init__(self, sum_val=0):
        self.inserts = []
        self.queries = []
        self._sum = sum_val

    def insert(self, stream, rows, column_names):
        self.inserts.append((stream, rows, column_names))

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        return type("R", (), {"result_rows": [(self._sum,)]})()


def test_day_window_is_utc_calendar_day():
    now = datetime(2026, 8, 17, 15, 30, 0, tzinfo=timezone.utc)
    start, reset = usage.day_window(now)
    assert start == datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
    assert reset == datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)


def test_record_usage_inserts_qualified_row(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    c = _FakeClient()
    ts = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    usage.record_usage(c, "bob", 1234, prefix="p_", ts=ts)
    stream, rows, cols = c.inserts[0]
    assert stream == "tpk.p_chat_usage"
    assert rows == [[ts, "bob", 1234]]
    assert cols == ["ts", "username", "tokens"]


def test_tokens_used_today_sums_and_scopes_query(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    c = _FakeClient(sum_val=5000)
    now = datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc)
    assert usage.tokens_used_today(c, "bob", prefix="p_", now=now) == 5000
    sql, params = c.queries[0]
    assert "table(tpk.p_chat_usage)" in sql
    assert params["u"] == "bob"
    assert params["start"] == datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)


def test_tokens_used_today_handles_null_sum(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_DATABASE", raising=False)
    c = _FakeClient(sum_val=None)  # sum() over no rows -> NULL
    assert usage.tokens_used_today(c, "bob", now=datetime.now(timezone.utc)) == 0


def test_db_usage_read_fails_open():
    def boom():
        raise RuntimeError("store down")
    store = usage.DbUsage("", boom)
    # Never block a user because the store is unreachable.
    assert store.used_today("bob") == 0


def test_db_usage_record_swallows_and_skips_zero():
    c = _FakeClient()
    store = usage.DbUsage("", lambda: c)
    store.record("bob", 0)      # non-positive -> no write
    assert c.inserts == []
    store.record("bob", 42)
    assert c.inserts and c.inserts[0][1] == [[c.inserts[0][1][0][0], "bob", 42]]
