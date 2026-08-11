"""Unit-level proof that KnowledgeGraph serializes all access to its shared
timeplus_connect client. Uses a fake client so it runs without a live
timeplusd (the live-DB smoke test lives in tests/test_tools.py::
test_search_entities_concurrent_calls_succeed, gated on TIMEPLUS_HOST).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tpk.tools import KnowledgeGraph


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class ConcurrencyTrackingClient:
    """Records whether more than one `query()` call is ever in flight at
    once. Raises from within an overlapping call rather than merely
    recording a flag, so a real timeplus_connect-style "concurrent queries
    within the same session" failure is reproduced deterministically instead
    of relying on timing-sensitive assertions after the fact.
    """

    def __init__(self, sleep_seconds: float = 0.05):
        self._sleep_seconds = sleep_seconds
        self._active = 0
        self._lock = threading.Lock()
        self.max_active = 0

    def query(self, sql, parameters=None):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active > 1:
                # Mirror timeplus_connect's real failure mode instead of
                # just recording a max: an overlapping call must blow up.
                self._active -= 1
                raise RuntimeError(
                    "Attempt to execute concurrent queries within the same "
                    "session.Please use a separate client instance per "
                    "thread/process."
                )
        try:
            time.sleep(self._sleep_seconds)
            return _FakeResult([])
        finally:
            with self._lock:
                self._active -= 1


def test_knowledge_graph_serializes_concurrent_search_entities():
    client = ConcurrencyTrackingClient(sleep_seconds=0.05)
    kg = KnowledgeGraph(client)

    errors = []

    def _search():
        try:
            kg.search_entities("checkpoint flush")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=_search) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert client.max_active == 1


def test_knowledge_graph_serializes_many_concurrent_calls():
    client = ConcurrencyTrackingClient(sleep_seconds=0.02)
    kg = KnowledgeGraph(client)

    def _search(i):
        kg.search_entities(f"query {i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_search, i) for i in range(8)]
        # Any exception (including the fake's overlap RuntimeError) surfaces
        # here via .result().
        for f in futures:
            f.result()

    assert client.max_active == 1
