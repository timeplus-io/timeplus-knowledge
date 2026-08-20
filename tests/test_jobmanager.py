import time

from tpk.config import RepoConfig
from tpk.ingest import IngestProgress, IngestResult


def _wait_done(mgr, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = mgr.get(job_id)
        if rec and rec["status"] in ("ok", "failed"):
            return rec
        time.sleep(0.02)
    return mgr.get(job_id)


def test_jobmanager_records_progress_fields(monkeypatch):
    import tpk.api as api_mod
    from tpk.api import JobManager

    # DB-free: the worker's db/llm calls are stubbed; ingest is faked to drive
    # the progress sink and return a success result.
    monkeypatch.setattr(api_mod.db, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(
        api_mod, "load_llm",
        lambda *a, **k: type("L", (), {"backend": "auto", "model": None, "token_budget": 0})(),
    )

    def fake_ingest(client, cfg, prefix="", backend=None, model=None,
                    token_budget=0, on_progress=None, **kw):
        from tpk.config import entry_key
        if on_progress:
            on_progress(IngestProgress("extract", message="extracting x.py"))
            on_progress(IngestProgress("done", nodes=3, edges=2))
        return IngestResult(entry_key(cfg), "j", 3, 2, "ok")

    monkeypatch.setattr(api_mod, "ingest_repo", fake_ingest)

    mgr = JobManager(prefix="")
    cfg = RepoConfig(name="docs", github="org/docs", ref="v1", visibility="internal", enabled=True)
    job_id = mgr.submit(cfg)

    # A freshly-submitted job carries the new keys immediately (queued).
    rec0 = mgr.get(job_id)
    assert rec0["phase"] is None and rec0["updated_at"] is None and rec0["message"] == ""

    rec = _wait_done(mgr, job_id)
    assert rec["status"] == "ok"
    assert rec["phase"] == "done"                 # last reported phase
    assert rec["updated_at"] is not None          # a progress event bumped it
    assert rec["message"] == "extracting x.py"
    assert rec["nodes"] == 3 and rec["edges"] == 2
    # snapshot() (what /api/jobs returns) exposes the same rec.
    snap = next(j for j in mgr.snapshot() if j["id"] == job_id)
    assert snap["phase"] == "done" and "updated_at" in snap
