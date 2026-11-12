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


def test_jobmanager_import_job_loads_bundle_and_cleans_up(monkeypatch, tmp_path):
    """#102: an import job runs transfer.import_bundle on the single worker,
    records per-stream rows, and deletes the unpacked bundle dir afterwards."""
    import tpk.api as api_mod
    from tpk.api import IMPORT_ENTRY_KEY, JobManager

    monkeypatch.setattr(api_mod.db, "get_client", lambda *a, **k: object())
    seen = {}

    def fake_import(client, in_dir, prefix="", replace=False, entries=None):
        seen["in_dir"], seen["replace"], seen["prefix"] = in_dir, replace, prefix
        seen["entries"] = entries
        return {"format": "Parquet", "streams": {"kg_nodes": {"rows": 7}, "kg_edges": {"rows": 4}}}

    monkeypatch.setattr(api_mod.transfer, "import_bundle", fake_import)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{}")

    mgr = JobManager(prefix="p_")
    job_id = mgr.submit_import(bundle, replace=True)
    rec = _wait_done(mgr, job_id)
    assert rec["status"] == "ok"
    assert rec["kind"] == "import" and rec["entry_key"] == IMPORT_ENTRY_KEY
    assert rec["streams"] == {"kg_nodes": 7, "kg_edges": 4}
    assert (rec["nodes"], rec["edges"]) == (7, 4)
    assert rec["finished_at"] is not None
    assert seen == {"in_dir": bundle, "replace": True, "prefix": "p_", "entries": None}
    assert rec["entries"] is None
    assert not bundle.exists()
    assert mgr.has_active() is False


def test_jobmanager_import_job_passes_selected_entries(monkeypatch, tmp_path):
    """#104: an entry-filtered import hands the keys to transfer.import_bundle
    and records them on the job."""
    import tpk.api as api_mod
    from tpk.api import JobManager

    monkeypatch.setattr(api_mod.db, "get_client", lambda *a, **k: object())
    seen = {}

    def fake_import(client, in_dir, prefix="", replace=False, entries=None):
        seen["replace"], seen["entries"] = replace, entries
        return {"format": "Parquet", "streams": {"kg_nodes": {"rows": 2}}}

    monkeypatch.setattr(api_mod.transfer, "import_bundle", fake_import)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    mgr = JobManager()
    rec = _wait_done(mgr, mgr.submit_import(bundle, replace=True, entries=["a@v1", "b@v2"]))
    assert rec["status"] == "ok"
    assert seen == {"replace": True, "entries": ["a@v1", "b@v2"]}
    assert rec["entries"] == ["a@v1", "b@v2"]
    assert rec["message"] == "replacing 2 selected entries"
    assert not bundle.exists()


def test_jobmanager_import_failure_is_recorded_and_cleaned(monkeypatch, tmp_path):
    import tpk.api as api_mod
    from tpk.api import JobManager

    monkeypatch.setattr(api_mod.db, "get_client", lambda *a, **k: object())

    def boom(client, in_dir, prefix="", replace=False, entries=None):
        raise ValueError("kg_nodes: target stream is missing column(s) ['x']")

    monkeypatch.setattr(api_mod.transfer, "import_bundle", boom)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    mgr = JobManager()
    rec = _wait_done(mgr, mgr.submit_import(bundle))
    assert rec["status"] == "failed"
    assert "missing column" in rec["error"]
    assert rec["finished_at"] is not None
    assert not bundle.exists()
