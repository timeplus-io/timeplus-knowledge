# Ingest Progress & Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `tpk ingest` (CLI) and the Ingest jobs panel (UI) live per-phase progress instead of an opaque multi-minute wait, driven by one backend progress mechanism.

**Architecture:** A progress callback (`IngestProgress`/`ProgressFn`) threaded through `ingest_repo`. `run_graphify` switches to `Popen` and forwards graphify's stdout line-by-line, so the long extract phase emits a live heartbeat. The JobManager (→UI, in-memory job record + `/api/jobs`) and the CLI each supply a sink. Honest-indeterminate: phase + elapsed + heartbeat + live node/edge counts; no fabricated totals.

**Tech Stack:** Python (subprocess, FastAPI, pytest), TypeScript/React (Vite), Typer CLI.

**Spec:** `docs/superpowers/specs/2026-08-20-ingest-progress-design.md`

## Global Constraints

- Phase vocabulary is exactly: `fetch`, `extract`, `parse`, `upsert`, `done`.
- `on_progress`/`on_line` default to `None` (no-op) — existing callers and tests must keep working unchanged.
- No `processed`/`total` fields anywhere (honest-indeterminate). No job persistence (in-memory as today).
- Heartbeat source is graphify's own stdout (tee), not a timer. Set `PYTHONUNBUFFERED=1` in graphify's child env.
- Stall threshold in the UI is `STALL_MS = 60_000`.
- All new backend tests are DB-free (stub-based) — including the JobManager test, which drives `JobManager` directly with `db.get_client`/`load_llm`/`ingest_repo` stubbed rather than going through the `requires_timeplus` HTTP+auth path.
- Every task ends with a passing test run (or build) and a commit.

---

### Task 1: `run_graphify` — tee graphify stdout via `Popen`

**Files:**
- Modify: `src/tpk/graphify_runner.py` (`run_graphify`, the subprocess block)
- Test: `tests/test_graphify_runner.py` (update the `subprocess.run` mocks to `Popen`; add a tee test)

**Interfaces:**
- Produces: `run_graphify(repo_path, out_dir, extraction="code-only", backend=None, model=None, token_budget=0, stream=False, on_line: Callable[[str], None] | None = None) -> Path`. Each line of graphify output is passed to `on_line` (newline-stripped) in order; `stream=True` also echoes lines to stdout. Child env includes `PYTHONUNBUFFERED=1`.

- [ ] **Step 1: Update the existing subprocess mocks to `Popen` and add the tee test**

The current tests patch `subprocess.run`; after this task `run_graphify` uses `Popen`. Replace the `_FakeProc`/`_capture_graphify` helpers and the missing-binary test in `tests/test_graphify_runner.py` with a fake `Popen`, and add a tee test:

```python
class _FakePopen:
    """Stand-in for subprocess.Popen: yields canned stdout lines, fabricates
    graph.json under --out, and reports returncode."""
    def __init__(self, cmd, stdout=None, stderr=None, text=None, env=None, bufsize=None):
        self.cmd = cmd
        self.env = env
        self.returncode = 0
        out = Path(cmd[cmd.index("--out") + 1])
        gj = out / "graphify-out" / "graph.json"
        gj.parent.mkdir(parents=True, exist_ok=True)
        gj.write_text('{"nodes": [], "links": []}')
        self.stdout = iter(["extracting app.py\n", "extracting util.py\n"])
    def wait(self):
        return self.returncode


def _capture_graphify(monkeypatch, calls, popen_cls=_FakePopen):
    """Fake subprocess.Popen that records argv/env and fabricates graph.json."""
    import tpk.graphify_runner as gr

    def fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return popen_cls(cmd, **kwargs)

    monkeypatch.setattr(gr.subprocess, "Popen", fake_popen)


def test_run_graphify_missing_binary_raises_graphify_error(tmp_path: Path, monkeypatch):
    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'graphify'")
    monkeypatch.setattr(graphify_runner.subprocess, "Popen", fake_popen)
    with pytest.raises(GraphifyError, match="graphify executable not found"):
        run_graphify(tmp_path / "repo", tmp_path / "out")


def test_run_graphify_forwards_stdout_lines_and_sets_unbuffered(monkeypatch, tmp_path: Path):
    calls: list = []
    _capture_graphify(monkeypatch, calls)
    seen: list[str] = []
    run_graphify(tmp_path, tmp_path / "out", on_line=seen.append)
    assert seen == ["extracting app.py", "extracting util.py"]  # newline-stripped, in order
    assert calls[0]["env"]["PYTHONUNBUFFERED"] == "1"


def test_run_graphify_nonzero_exit_raises_with_output_tail(monkeypatch, tmp_path: Path):
    class _FailPopen(_FakePopen):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.returncode = 1
            self.stdout = iter(["boom line 1\n", "boom line 2\n"])
    calls: list = []
    _capture_graphify(monkeypatch, calls, popen_cls=_FailPopen)
    with pytest.raises(GraphifyError, match="boom line 2"):
        run_graphify(tmp_path, tmp_path / "out")
```

Keep the existing `test_run_graphify_code_only_argv`, `..._semantic_argv_with_backends`, and `..._semantic_fails_fast_without_keys` — they call `_capture_graphify` and inspect `calls[i]["cmd"]`, which the new fake still records.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_graphify_runner.py -q`
Expected: FAIL — `run_graphify` still calls `subprocess.run` (mock patches `Popen`, so the real `run` is hit or `on_line` is unknown).

- [ ] **Step 3: Rewrite the subprocess block in `run_graphify`**

In `src/tpk/graphify_runner.py`, add `on_line` to the signature and replace the `subprocess.run(...)` block. Add `from collections import deque` at the top.

```python
def run_graphify(
    repo_path: Path,
    out_dir: Path,
    extraction: str = "code-only",
    backend: str | None = None,
    model: str | None = None,
    token_budget: int = 0,
    stream: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> Path:
```

Replace the `try: proc = subprocess.run(...) ... if proc.returncode != 0:` block with:

```python
    child_env = _child_env_with_placeholder_key()
    # Force graphify (a Python CLI) to line-flush stdout so the tee below is a
    # live heartbeat over a pipe, not a single end-of-run batch.
    child_env["PYTHONUNBUFFERED"] = "1"
    recent: deque[str] = deque(maxlen=50)
    try:
        # Merge stderr into stdout so graphify's progress (on either stream) is
        # captured in order. Read line-by-line: each line is forwarded to
        # `on_line` (the extract-phase heartbeat) and, when stream=True, echoed
        # to the terminal (preserving `tpk ingest --verbose`).
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env,
        )
    except FileNotFoundError as exc:
        raise GraphifyError(
            "graphify executable not found on PATH; ensure the `graphifyy` package "
            "(providing the `graphify` CLI) is installed in this environment"
        ) from exc
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        recent.append(line)
        if on_line is not None:
            on_line(line)
        if stream:
            print(line)
    proc.wait()
    if proc.returncode != 0:
        detail = ("\n".join(recent))[-2000:] or "(no output captured)"
        raise GraphifyError(f"graphify failed on {repo_path}: {detail}")
```

Add `from collections.abc import Callable` (or `from typing import Callable`) to the imports if not present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_graphify_runner.py -q`
Expected: PASS (all — existing argv/parse tests plus the three new/updated tests).

- [ ] **Step 5: Commit**

```bash
git add src/tpk/graphify_runner.py tests/test_graphify_runner.py
git commit -m "feat(graphify): tee graphify stdout line-by-line via Popen (#64)"
```

---

### Task 2: Progress interface + thread through `ingest_repo`

**Files:**
- Modify: `src/tpk/ingest.py` (`IngestProgress`, `ProgressFn`, `ingest_repo`)
- Test: Create `tests/test_ingest_progress.py` (DB-free — no `requires_timeplus`)

**Interfaces:**
- Consumes: `run_graphify(..., on_line=...)` (Task 1).
- Produces: `IngestProgress(phase, message="", nodes=0, edges=0)` dataclass; `ProgressFn = Callable[[IngestProgress], None]`; `ingest_repo(..., on_progress: ProgressFn | None = None)` fires phase events `fetch?`→`extract`→`parse`→`upsert`→`done`, one `extract` event per graphify line, and counts from `parse` onward.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_progress.py` (no `requires_timeplus`, so it runs without a DB by stubbing graphify/parse/upsert/log):

```python
from pathlib import Path

import tpk.ingest as ingest_mod
from tpk.config import RepoConfig
from tpk.ingest import IngestProgress, ingest_repo
from tpk.model import Edge, Node


def test_ingest_repo_reports_phase_sequence(monkeypatch, tmp_path: Path):
    # Stub graphify to emit two heartbeat lines, and parse to a known graph.
    def fake_run_graphify(repo_path, out_dir, on_line=None, **kw):
        for ln in ("extracting a.py", "extracting b.py"):
            if on_line:
                on_line(ln)
        gj = tmp_path / "graph.json"
        gj.write_text("{}")
        return gj

    nodes = [Node(id="n1", repo="r", kind="function", name="f",
                  qualified_name="m.f", file_path="m.py", line_start=1, line_end=2,
                  summary="s", community="c", visibility="internal")]
    edges: list[Edge] = []
    monkeypatch.setattr(ingest_mod, "run_graphify", fake_run_graphify)
    monkeypatch.setattr(ingest_mod, "parse_graph_json", lambda *a, **k: (nodes, edges))
    monkeypatch.setattr(ingest_mod, "upsert_graph", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_git_sha", lambda *a, **k: "sha")

    events: list[IngestProgress] = []
    cfg = RepoConfig(name="r", path=tmp_path, ref="", visibility="internal", enabled=True)
    result = ingest_repo(object(), cfg, prefix="", out_root=tmp_path / "out",
                         on_progress=events.append)

    assert result.status == "ok" and result.nodes == 1 and result.edges == 0
    phases = [e.phase for e in events]
    # local-path repo -> no fetch; extract (incl. 2 heartbeats) -> parse -> upsert -> done
    assert phases[0] == "extract"
    assert phases.count("extract") >= 3          # start + 2 line heartbeats
    assert [p for p in ("parse", "upsert", "done") if p in phases] == ["parse", "upsert", "done"]
    assert [e.message for e in events if e.phase == "extract" and e.message] == \
        ["extracting a.py", "extracting b.py"]
    # counts are known from parse onward
    done = next(e for e in events if e.phase == "done")
    assert done.nodes == 1 and done.edges == 0


def test_ingest_repo_no_progress_callback_is_noop(monkeypatch, tmp_path: Path):
    # Default on_progress=None must not raise.
    monkeypatch.setattr(ingest_mod, "run_graphify", lambda *a, **k: tmp_path / "g.json")
    monkeypatch.setattr(ingest_mod, "parse_graph_json", lambda *a, **k: ([], []))
    monkeypatch.setattr(ingest_mod, "upsert_graph", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ingest_mod, "_git_sha", lambda *a, **k: "sha")
    cfg = RepoConfig(name="r", path=tmp_path, ref="", visibility="internal", enabled=True)
    assert ingest_repo(object(), cfg, out_root=tmp_path / "o").status == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_progress.py -q`
Expected: FAIL with `ImportError` (no `IngestProgress`) / `TypeError` (`ingest_repo` has no `on_progress`).

- [ ] **Step 3: Implement in `src/tpk/ingest.py`**

Add imports and the dataclass near the top (after the existing imports):

```python
from collections.abc import Callable


@dataclass(frozen=True)
class IngestProgress:
    phase: str            # "fetch" | "extract" | "parse" | "upsert" | "done"
    message: str = ""
    nodes: int = 0
    edges: int = 0


ProgressFn = Callable[[IngestProgress], None]
```

Add `on_progress` to `ingest_repo` and fire events. Replace the `try:` body:

```python
def ingest_repo(
    client,
    repo_cfg: RepoConfig,
    prefix: str = "",
    out_root: Path = Path(".graphify_out"),
    backend: str | None = None,
    model: str | None = None,
    token_budget: int = 0,
    stream: bool = False,
    on_progress: ProgressFn | None = None,
) -> IngestResult:
    run_id = uuid.uuid4().hex[:12]
    run_started_at = datetime.now(timezone.utc)
    key = entry_key(repo_cfg)
    repo_path = repo_cfg.path

    def _report(phase: str, message: str = "", nodes: int = 0, edges: int = 0) -> None:
        if on_progress is not None:
            on_progress(IngestProgress(phase, message, nodes, edges))

    try:
        if repo_cfg.github:
            _report("fetch")
            repo_path = fetch_github_repo(repo_cfg)
        _report("extract")
        graph_json = run_graphify(
            repo_path,
            out_root / key.replace("/", "_"),
            extraction=repo_cfg.extraction,
            backend=backend,
            model=model,
            token_budget=token_budget,
            stream=stream,
            on_line=lambda line: _report("extract", message=line),
        )
        _report("parse")
        nodes, edges = parse_graph_json(graph_json, key, repo_cfg.visibility)
        _report("upsert", nodes=len(nodes), edges=len(edges))
        upsert_graph(client, prefix, nodes, edges, run_started_at, repos={key, repo_cfg.name})
        result = IngestResult(key, run_id, len(nodes), len(edges), "ok")
        _report("done", nodes=len(nodes), edges=len(edges))
    except Exception as exc:  # per-repo isolation: never propagate, never touch prior rows
        print(f"[tpk] ingest failed for {key}: {exc}")
        result = IngestResult(key, run_id, 0, 0, "failed")
    sha = _git_sha(repo_path) if repo_path else "unknown"
    if repo_cfg.ref:
        sha = f"{sha} ({repo_cfg.ref})"
    _log(client, prefix, result, sha)
    return result
```

(The comment about `repos={key, repo_cfg.name}` from the original is preserved by keeping that argument.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingest_progress.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tpk/ingest.py tests/test_ingest_progress.py
git commit -m "feat(ingest): IngestProgress callback threaded through ingest_repo (#64)"
```

---

### Task 3: JobManager progress fields + API exposure

**Files:**
- Modify: `src/tpk/api.py` (`JobManager.submit` rec, `JobManager._run` sink)
- Test: Create `tests/test_jobmanager.py` (DB-free — tests `JobManager` directly, no `requires_timeplus`)

**Interfaces:**
- Consumes: `ingest_repo(..., on_progress=...)` (Task 2).
- Produces: job records carry `phase` (str|None), `message` (str), `updated_at` (ISO str|None), with live `nodes`/`edges`. `snapshot()`/`get()` return the whole rec unchanged, so `/api/jobs` and `/api/jobs/{id}` expose the fields with no endpoint change.

- [ ] **Step 1: Write the failing test**

The `/api/jobs` HTTP path is `requires_timeplus` (auth/sessions need a DB) and a local timeplusd may be unavailable, so test the `JobManager` directly — its worker thread runs `_run`, which we make DB-free by stubbing `db.get_client`, `load_llm`, and `ingest_repo`. Create `tests/test_jobmanager.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_jobmanager.py -q`
Expected: FAIL — the rec has no `phase`/`updated_at`/`message` keys (`KeyError`), and `ingest_repo` isn't yet called with `on_progress`.

- [ ] **Step 3: Implement in `src/tpk/api.py`**

Extend the rec in `JobManager.submit`:

```python
        rec = {
            "id": job_id, "entry_key": entry_key(cfg), "status": "queued",
            "phase": None, "message": "", "nodes": 0, "edges": 0, "error": None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "finished_at": None,
        }
```

In `JobManager._run`, build a progress sink and pass it to `ingest_repo`. Inside the `try` (after `rec["status"] = "running"`), before the `ingest_repo(...)` call:

```python
            def _on_progress(p, _rec=rec):
                with self._jobs_lock:
                    _rec["phase"] = p.phase
                    if p.message:
                        _rec["message"] = p.message
                    if p.nodes:
                        _rec["nodes"] = p.nodes
                    if p.edges:
                        _rec["edges"] = p.edges
                    _rec["updated_at"] = datetime.now(timezone.utc).isoformat()

            result = ingest_repo(
                client, cfg, prefix=self.prefix, backend=backend,
                model=(llm.model or None) if llm else None,
                token_budget=llm.token_budget if llm else 0,
                on_progress=_on_progress,
            )
```

Add `from tpk.ingest import IngestProgress` only if referenced for typing — the sink takes `p` structurally, so an import is optional. Leave the terminal block (sets `status`/`nodes`/`edges`/`error`/`finished_at` from the result) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_jobmanager.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/api.py tests/test_jobmanager.py
git commit -m "feat(api): expose ingest job phase/message/updated_at + live counts (#64)"
```

---

### Task 4: UI — phase label, live counts, elapsed, stalled hint

**Files:**
- Modify: `web/src/Manage.tsx` (Job type, active-job rendering, helpers)

**Interfaces:**
- Consumes: `/api/jobs` fields `phase`, `message`, `updated_at`, live `nodes`/`edges` (Task 3).

> No frontend unit-test harness exists in `web/`. Verify with `npm --prefix web run build` (tsc + vite) and the manual check.

- [ ] **Step 1: Extend the Job type and add helpers**

In `web/src/Manage.tsx`, find the job type (fields `id`, `entry_key`, `status`, `nodes`, `edges`, `error`, `submitted_at`, `finished_at`) and add:

```typescript
  phase?: string | null;
  message?: string;
  updated_at?: string | null;
```

Add near the other helpers (`formatStarted`/`formatRelative`):

```typescript
const PHASE_LABEL: Record<string, string> = {
  fetch: "fetching…", extract: "extracting…", parse: "parsing…", upsert: "saving…",
};
const STALL_MS = 60_000;

function phaseLabel(phase?: string | null): string {
  return (phase && PHASE_LABEL[phase]) || "running…";
}
function isStalled(updatedAt: string | null | undefined, now: number): boolean {
  if (!updatedAt) return false;               // queued / not yet reported
  return now - new Date(updatedAt).getTime() > STALL_MS;
}
function elapsedLabel(sinceIso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(sinceIso).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}
```

- [ ] **Step 2: Add a 1s ticker so elapsed advances between the 5s polls**

Inside the component, alongside the existing state/effects:

```typescript
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
```

- [ ] **Step 3: Render phase, live counts, elapsed, and the stalled hint**

Replace the running-job block (currently `j.status === "running" ? (bar + "running…") : "queued…"`) with:

```tsx
                    {j.status === "running" ? (
                      <>
                        <div className="tk-job-bar-track"><span className="tk-job-bar-fill" /></div>
                        <div className="tk-job-status-running">
                          {isStalled(j.updated_at, now) ? (
                            <span className="tk-job-stalled">stalled?</span>
                          ) : (
                            phaseLabel(j.phase)
                          )}
                          {(j.nodes > 0 || j.edges > 0) && (
                            <span className="tk-job-counts"> · {j.nodes} nodes · {j.edges} edges</span>
                          )}
                          <span className="tk-job-elapsed"> · {elapsedLabel(j.submitted_at, now)}</span>
                        </div>
                      </>
                    ) : (
                      <div className="tk-job-status-running">queued…</div>
                    )}
```

Add minimal CSS for `.tk-job-stalled` (muted/amber) and `.tk-job-counts`/`.tk-job-elapsed` (secondary text) in the same stylesheet the other `tk-job-*` classes live in — match the existing muted-text pattern; do not invent a new palette.

- [ ] **Step 4: Type-check and build**

Run: `npm --prefix web run build`
Expected: PASS (`tsc -b` clean, `vite build` completes). Fix any type mismatch on the new optional Job fields.

- [ ] **Step 5: Manual verification (record in the PR)**

Trigger a reindex; while it runs the panel shows a phase label (e.g. "extracting…"), node/edge counts appear once > 0, elapsed ticks every second, and if `updated_at` goes stale for 60s the row shows "stalled?". The finished line is unchanged.

- [ ] **Step 6: Commit**

```bash
git add web/src/Manage.tsx web/src/*.css
git commit -m "feat(web): show ingest phase, live counts, elapsed + stalled hint (#64)"
```

---

### Task 5: CLI — advancing progress during extract

**Files:**
- Modify: `src/tpk/cli.py` (the `ingest` command loop; add a small progress-line formatter)
- Test: `tests/test_cli.py` (unit-test the pure formatter)

**Interfaces:**
- Consumes: `ingest_repo(..., on_progress=...)` (Task 2), `IngestProgress` (Task 2).
- Produces: `_progress_line(i, n, key, phase, elapsed_s, message)` — a pure formatter returning the status string; the command wires an `on_progress` sink that renders it (refreshing on a TTY, throttled plain lines otherwise). `--verbose` suppresses the status line (graphify streams its own output).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
from tpk.cli import _progress_line


def test_progress_line_format():
    line = _progress_line(1, 4, "docs@main", "extract", 90, "extracting x.py")
    assert line.startswith("[1/4] docs@main")
    assert "extracting…" in line          # phase label, not the raw phase key
    assert "1m30s" in line                # elapsed formatting
    assert "extracting x.py" in line      # last graphify message


def test_progress_line_truncates_long_message():
    line = _progress_line(1, 1, "r", "extract", 1, "x" * 500)
    assert len(line) <= 200               # bounded so it fits one terminal line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_progress_line_format tests/test_cli.py::test_progress_line_truncates_long_message -q`
Expected: FAIL — `_progress_line` does not exist.

- [ ] **Step 3: Implement in `src/tpk/cli.py`**

Add the pure formatter and phase labels near the top of the module:

```python
import sys
import time

_PHASE_LABEL = {"fetch": "fetching…", "extract": "extracting…",
                "parse": "parsing…", "upsert": "saving…", "done": "done"}


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def _progress_line(i: int, n: int, key: str, phase: str, elapsed_s: float,
                   message: str = "") -> str:
    label = _PHASE_LABEL.get(phase, phase)
    parts = [f"[{i}/{n}] {key}", label, _fmt_elapsed(elapsed_s)]
    if message:
        parts.append(message)
    line = " · ".join(parts)
    return line[:197] + "…" if len(line) > 200 else line
```

Wire it into the `ingest` command loop. Replace the body of the `for i, cfg in enumerate(entries, 1):` loop:

```python
    for i, cfg in enumerate(entries, 1):
        typer.echo(f"[{i}/{len(entries)}] {entry_key(cfg)}: extracting ({cfg.extraction})...")
        started = time.monotonic()
        tty = sys.stdout.isatty()
        last_emit = [0.0]

        def _on_progress(p, _i=i, _cfg=cfg, _started=started, _last=last_emit):
            if verbose:
                return  # graphify's own output is already streaming
            elapsed = time.monotonic() - _started
            line = _progress_line(_i, len(entries), entry_key(_cfg), p.phase, elapsed, p.message)
            if tty:
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
            elif elapsed - _last[0] >= 5 or p.phase in ("parse", "upsert", "done"):
                _last[0] = elapsed
                typer.echo(line)

        result = ingest_repo(
            client, cfg, prefix=prefix, backend=backend, model=model,
            token_budget=llm.token_budget, stream=verbose, on_progress=_on_progress,
        )
        if tty and not verbose:
            sys.stdout.write("\r\033[K")  # clear the refreshing line
            sys.stdout.flush()
        typer.echo(f"{result.repo}: {result.status} ({result.nodes} nodes, {result.edges} edges)")
```

Add `from tpk.ingest import IngestResult, ingest_repo` already exists; no new import beyond `sys`/`time`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: PASS (new formatter tests plus existing CLI tests).

- [ ] **Step 5: Commit**

```bash
git add src/tpk/cli.py tests/test_cli.py
git commit -m "feat(cli): advancing per-phase ingest progress line (#64)"
```

---

### Task 6: Verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Backend suites for this change**

Run: `.venv/bin/python -m pytest tests/test_graphify_runner.py tests/test_ingest_progress.py tests/test_jobmanager.py tests/test_cli.py -q`
Expected: PASS (all DB-free).

- [ ] **Step 2: Frontend build**

Run: `npm --prefix web run build`
Expected: PASS.

- [ ] **Step 3: Manual graphify-buffering check (record result in PR)**

Run a real small ingest and confirm graphify lines stream live (CLI status line advances mid-extract; UI heartbeat bumps `updated_at`) rather than arriving in one batch at the end — validating the `PYTHONUNBUFFERED=1` assumption. If graphify batches on a pipe, note it: the heartbeat degrades to phase-boundary granularity (still an improvement); no code path breaks.

- [ ] **Step 4: Open the PR**

Push and open a PR referencing #64. Summarize the one progress mechanism, the graphify tee + `PYTHONUNBUFFERED=1`, honest-indeterminate progress, and the manual-verification result from Step 3.

---

## Self-Review

**Spec coverage:**
- Progress interface + pipeline threading → Task 2.
- graphify tee (Popen, per-line forward, `PYTHONUNBUFFERED=1`, ring-buffer error) → Task 1.
- Job record fields + API exposure → Task 3.
- UI phase label / live counts / elapsed / 60s stall → Task 4.
- CLI refreshing/periodic progress, `--verbose` unchanged → Task 5.
- Honest-indeterminate (no `processed`/`total`), in-memory only → held across Tasks 2–4 (no such fields introduced).
- Buffering verification → Task 6 Step 3.

**Placeholder scan:** No TODO/TBD; every code and test step is concrete.

**Type/name consistency:** `IngestProgress(phase, message, nodes, edges)` and `ProgressFn` defined in Task 2 and consumed with the same shape in Tasks 3 (`_on_progress(p)`) and 5 (`_on_progress(p)`). `run_graphify(..., on_line=...)` defined in Task 1 and called in Task 2. Phase strings (`fetch|extract|parse|upsert|done`) and labels are consistent across CLI (`_PHASE_LABEL`) and UI (`PHASE_LABEL`). Job fields (`phase`/`message`/`updated_at`) named identically in Task 3 (backend) and Task 4 (Job type).
