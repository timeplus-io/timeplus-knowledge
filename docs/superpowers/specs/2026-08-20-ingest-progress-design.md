# Ingest progress & status design (#64)

## Problem

Ingest gives almost no live feedback while it runs, on both surfaces:

- **Web UI** — the *Ingest jobs* panel shows only an indeterminate bar +
  `running…` for a job's entire duration (`web/src/Manage.tsx`).
- **CLI** — `tpk ingest` prints one line per repo, then goes silent through the
  whole extraction (`src/tpk/cli.py`).

A single repo's ingest (git checkout + LLM semantic extraction) can run for many
minutes, so both surfaces look **stuck** — no signal of phase, progress, or
liveness.

## Root cause

The pipeline emits no progress mid-run. `ingest_repo` runs opaquely —
graphify (checkout + extraction) → `parse_graph_json` → `upsert_graph` → log —
and returns an `IngestResult` only when done (`src/tpk/ingest.py`). The job
record's `nodes`/`edges` are populated only at the end from that result
(`src/tpk/api.py`). Both consumers are starved by the same gap.

The defining constraint: `run_graphify` shells out to the `graphify` CLI as a
**single opaque blocking subprocess** (`subprocess.run`). The multi-minute
"extract" phase is one call; the pipeline captures nothing structured from it.
graphify prints its own progress to stdout — surfaced today only via
`--verbose` (`stream=True`, which inherits the terminal). The other phases
(fetch → parse → upsert) are quick and their boundaries are trivial to report.
Node/edge counts are known right after `parse`.

## Decisions (locked)

- **Honest indeterminate.** During extract we do not fabricate a determinate
  bar. We surface phase + elapsed + a live heartbeat + node/edge counts once
  known. No `processed`/`total` fields. (graphify exposes no reliable per-file
  total without coupling to its stdout format.)
- **Heartbeat from graphify's own stdout.** Capture graphify's output
  line-by-line; each line is a heartbeat (bumps `updated_at`) and a "last
  activity" message. If graphify truly hangs, output stops → `updated_at`
  freezes → the UI honestly shows "stalled". Preferred over a timer, which
  would report false liveness through a hang.
- **One backend mechanism feeds both surfaces** — a progress callback threaded
  through the pipeline; the JobManager (→UI) and the CLI each supply a sink.
- **Out of scope:** determinate per-file bar, `processed`/`total`, job
  persistence across restart (jobs stay in-memory as today).

## Design

### 1. Progress interface (`src/tpk/ingest.py`)

```python
@dataclass(frozen=True)
class IngestProgress:
    phase: str            # "fetch" | "extract" | "parse" | "upsert" | "done"
    message: str = ""     # last graphify activity line / short human status
    nodes: int = 0
    edges: int = 0

ProgressFn = Callable[[IngestProgress], None]
```

`ingest_repo(..., on_progress: ProgressFn | None = None)` fires it:

- `IngestProgress("fetch")` before `fetch_github_repo` (github repos only).
- `IngestProgress("extract")` before `run_graphify`, then one
  `IngestProgress("extract", message=line)` per graphify output line.
- `IngestProgress("parse")` before `parse_graph_json`.
- `IngestProgress("upsert", nodes=N, edges=M)` before `upsert_graph` (counts are
  known after parse).
- `IngestProgress("done", nodes=N, edges=M)` at the end.

`on_progress` defaults to `None` (a no-op), so existing callers and tests are
unaffected. Failures keep the current behavior: `ingest_repo` still returns
`IngestResult(status="failed")`; the job record's `error` is set from the
result, and no further progress event is emitted.

### 2. graphify tee (`src/tpk/graphify_runner.py`)

Switch `run_graphify` from `subprocess.run` to `subprocess.Popen` and read
output line-by-line:

- `Popen(cmd, stdout=PIPE, stderr=STDOUT, text=True, env=child_env)` — merge
  stderr into stdout so graphify's progress (which may go to either) is
  captured in order.
- Add `on_line: Callable[[str], None] | None = None`. Iterate
  `for line in proc.stdout:` — call `on_line(line.rstrip())` for each line, and
  when `stream=True` also echo it to the terminal (`print(line, end="")`),
  preserving today's `--verbose` behavior.
- Keep a bounded ring buffer (e.g. `collections.deque(maxlen=50)`) of recent
  lines; on non-zero exit, the error detail is the joined tail (replacing
  today's `proc.stderr[-2000:]`).
- After the loop, `proc.wait()`; check `returncode` and `graph.json` existence
  exactly as today.

**Buffering (the crux — must verify).** A child's stdout over a pipe is often
block-buffered, which would defeat the live heartbeat. graphify is a Python CLI
(`graphifyy`), so add `PYTHONUNBUFFERED=1` to `child_env` to force line-flushed
output. This is verified during implementation against a real semantic run (see
Testing). If graphify still batches output on a pipe, the heartbeat degrades
gracefully to phase-boundary granularity — still strictly better than today, and
no other code path changes.

`ingest_repo` wires `on_line=lambda line: on_progress(IngestProgress("extract",
message=line))` so each graphify line becomes an extract heartbeat.

### 3. Job record + API (`src/tpk/api.py`)

Extend the in-memory job rec created in `JobManager.submit` with:

- `phase: str | None` (None while queued; set as phases report)
- `message: str` (last activity line; `""` default)
- `updated_at: str | None` (ISO timestamp; bumped on every progress event)

and update `nodes`/`edges` live (not just at the end).

In `JobManager._run`, build an `on_progress` sink that updates the rec under
`self._jobs_lock` (phase, message, nodes, edges, and `updated_at =
datetime.now(timezone.utc).isoformat()`, the same form `submitted_at`/
`finished_at` already use), and pass it to `ingest_repo(..., on_progress=...)`. Progress events can be frequent
(one per graphify line) but each is a cheap locked dict update; no throttling
needed at these volumes. `snapshot()`/`get()` already return the whole rec, so
`/api/jobs` and `/api/jobs/{id}` expose the new fields with no endpoint change.

### 4. UI (`web/src/Manage.tsx`)

For active (running) jobs, replace the bare `running…` with:

- **Phase label** from `phase` (`{fetch: "fetching…", extract: "extracting…",
  parse: "parsing…", upsert: "saving…"}`, fallback `running…`).
- **Live nodes/edges** once `> 0` (e.g. `· 1,204 nodes · 3,881 edges`).
- **Elapsed** since `submitted_at`, updated by a local 1s ticker (independent of
  the 5s data poll) so the timer looks alive between polls.
- **Stalled hint** when `updated_at` is older than a threshold (`STALL_MS =
  30_000`) — a distinct visual (e.g. a muted "stalled?" tag) so a hung job is
  visibly different from an active one. Jobs with no `updated_at` yet (just
  queued) are not "stalled".
- Keep the **indeterminate** bar.

The finished line (`ok · N nodes · M edges` / `failed · …`) is unchanged.

### 5. CLI (`src/tpk/cli.py`)

Pass an `on_progress` sink to `ingest_repo` that renders per repo:

- **TTY** (`sys.stdout.isatty()`): a single refreshing status line via `\r` —
  `[{i}/{N}] {key} · {phase} · {elapsed} · {last message, truncated}` — cleared
  with a newline when the repo finishes (then the existing final line prints).
- **Non-TTY** (piped/redirected): periodic plain lines, throttled (e.g. emit on
  phase change and at most every ~5s within a phase) to avoid spamming logs.
- `--verbose` is unchanged: graphify's full output still streams to the
  terminal (the tee echoes each line); the refreshing status line is suppressed
  in verbose mode to avoid fighting graphify's own output.

The pre-repo `[i/N] … extracting…` and post-repo `{status} (N nodes, M edges)`
lines are preserved.

## Testing

- **`ingest_repo` progress sequence** — monkeypatch `run_graphify` and
  `upsert_graph`; assert `on_progress` is called with phases in order
  `fetch?` → `extract` → `parse` → `upsert` → `done`, and that `nodes`/`edges`
  are populated from `parse` onward. (`fetch` only for a github-configured
  repo.)
- **`run_graphify` tee** — with a fake `graphify` on PATH (a shell/python stub
  that prints known lines then writes a minimal `graphify-out/graph.json`),
  assert every printed line reaches `on_line` in order, the returned path is the
  graph.json, and a non-zero exit raises `GraphifyError` whose detail contains
  the tail lines. Assert `PYTHONUNBUFFERED=1` is set in the child env.
- **JobManager** — submit a job with a stubbed `ingest_repo` that invokes its
  `on_progress`; assert the rec gains `phase`/`message`/`updated_at` and live
  `nodes`/`edges`, and that `snapshot()` exposes them.
- **API** — `/api/jobs` returns the new fields for an in-flight job (using the
  stubbed manager).
- **Frontend** — `tsc -b && vite build`; phase-label / stalled-tag logic is a
  small pure helper that can be unit-checked if a harness exists, else manual.
- **Manual (graphify buffering)** — a real `tpk ingest --repo <small>` run to
  confirm graphify lines stream live (heartbeat advances) rather than arriving
  in one batch at the end. This validates the `PYTHONUNBUFFERED=1` assumption.

## Touched files

- `src/tpk/ingest.py` — `IngestProgress`, `ProgressFn`, `on_progress` threading.
- `src/tpk/graphify_runner.py` — `Popen` tee, `on_line`, `PYTHONUNBUFFERED=1`,
  ring-buffer error detail.
- `src/tpk/api.py` — job rec fields + `on_progress` sink in `JobManager._run`.
- `web/src/Manage.tsx` — phase label, live counts, elapsed ticker, stalled hint.
- `src/tpk/cli.py` — refreshing/periodic progress sink.
- Tests alongside the backend modules above.
