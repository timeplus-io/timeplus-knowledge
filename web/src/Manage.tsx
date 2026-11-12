import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { peekManifest, type BundleManifest } from "./bundle";
import { CAP, hasCap } from "./capabilities";

type Repo = {
  name: string; ref: string; entry_key: string; github: string; path: string;
  enabled: boolean; visibility: string; extraction: string; description: string;
  node_count: number; last_status: string | null; last_sha: string | null;
  last_time: string | null;
};
type Job = {
  id: string; entry_key: string; status: string; nodes: number; edges: number;
  error: string | null; submitted_at: string; finished_at: string | null;
  phase?: string | null;
  message?: string;
  updated_at?: string | null;
  // "ingest" (one entry) or "import" (a whole bundle, #102); import jobs
  // report per-stream row counts instead of an ingest-log row.
  kind?: string;
  streams?: Record<string, number>;
  // import jobs: the entry keys the load was limited to (null = whole bundle), #104
  entries?: string[] | null;
};

// One ingest run from the persistent log (#15): UI, CLI and kubectl runs alike.
type Run = {
  run_id: string; entry_key: string; status: string; nodes: number; edges: number;
  git_sha: string; error: string; started_at: string | null; finished_at: string | null;
  duration_s: number | null;
};

function durationLabel(s: number | null): string {
  if (s === null) return "";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${Math.round(s - m * 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

const EMPTY_FORM = { name: "", github: "", ref: "", path: "", visibility: "internal",
  extraction: "code-only", description: "" };

async function api(path: string, body?: unknown) {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await apiFetch(path, body === undefined ? { headers } : {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    if (resp.status === 401) throw new Error("Unauthorized — please sign in again");
    throw new Error(`${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

// The API has no clock-skew-safe "now" — Date.now() against the server's
// ISO timestamps is good enough for a relative-time label in an admin panel.
function formatStarted(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function formatRelative(iso: string): string {
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  return `${days}d ago`;
}

const PHASE_LABEL: Record<string, string> = {
  fetch: "fetching…", extract: "extracting…", parse: "parsing…", upsert: "saving…",
  import: "importing…",
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

// Pull a download filename out of `Content-Disposition: attachment;
// filename="x"`; falls back to a generic name if the header is absent.
function downloadName(resp: Response, fallback: string): string {
  const m = /filename="?([^";]+)"?/.exec(resp.headers.get("content-disposition") ?? "");
  return m ? m[1] : fallback;
}

// Checkbox list of entry keys with select-all / none, shared by the Export
// and Import modals (#104).
function EntryPicker({ keys, selected, onChange, disabled }: {
  keys: string[]; selected: Set<string>; onChange: (next: Set<string>) => void; disabled?: boolean;
}) {
  function toggle(key: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(key); else next.delete(key);
    onChange(next);
  }
  return (
    <div className="tk-entry-picker">
      <div className="tk-entry-picker-bar">
        <span>{selected.size} of {keys.length} selected</span>
        <span className="tk-entry-picker-bar-spacer" />
        <button type="button" className="tk-link-btn" disabled={disabled} onClick={() => onChange(new Set(keys))}>All</button>
        <button type="button" className="tk-link-btn" disabled={disabled} onClick={() => onChange(new Set())}>None</button>
      </div>
      <div className="tk-entry-picker-list">
        {keys.map((k) => (
          <label key={k} className="tk-entry-picker-item">
            <input type="checkbox" checked={selected.has(k)} disabled={disabled}
                   onChange={(e) => toggle(k, e.target.checked)} />
            {" "}<code>{k}</code>
          </label>
        ))}
      </div>
    </div>
  );
}

export default function Manage({ capabilities, isAdmin }: { capabilities: string[]; isAdmin?: boolean }) {
  // corpus:manage gates every mutation; with only corpus:view the screen is
  // read-only. Export/import of the WHOLE graph is admin-only (#102): a
  // corpus:manage role may be scoped to a subset of entries.
  const canManage = hasCap(capabilities, CAP.corpusManage);
  const canTransfer = !!isAdmin;
  const [repos, setRepos] = useState<Repo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [purge, setPurge] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importReplace, setImportReplace] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  // Entry scope for import (#104): the bundle's manifest is read client-side
  // from the chosen archive; "selected" is only offered when it lists entries.
  const [importManifest, setImportManifest] = useState<BundleManifest | null>(null);
  const [importPeek, setImportPeek] = useState<"idle" | "reading" | "ok" | "none">("idle");
  const [importScope, setImportScope] = useState<"all" | "selected">("all");
  const [importSel, setImportSel] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportScope, setExportScope] = useState<"all" | "selected">("all");
  const [exportSel, setExportSel] = useState<Set<string>>(new Set());

  function openAdd() { setForm({ ...EMPTY_FORM }); setError(""); setAddOpen(true); }
  function closeAdd() { setAddOpen(false); setError(""); }
  function openImport() {
    setImportFile(null); setImportReplace(false); setImportManifest(null); setImportPeek("idle");
    setImportScope("all"); setImportSel(new Set()); setError(""); setImportOpen(true);
  }
  function closeImport() { if (importBusy) return; setImportOpen(false); setError(""); }
  function openExport() { setExportScope("all"); setExportSel(new Set()); setError(""); setExportOpen(true); }
  function closeExport() { if (exporting) return; setExportOpen(false); setError(""); }

  async function chooseImportFile(f: File | null) {
    setImportFile(f); setImportManifest(null); setImportScope("all"); setImportSel(new Set());
    if (!f) { setImportPeek("idle"); return; }
    setImportPeek("reading");
    const m = await peekManifest(f);
    setImportManifest(m);
    setImportPeek(m && Array.isArray(m.entries) ? "ok" : "none");
  }
  const bundleEntries: string[] = importPeek === "ok" ? (importManifest?.entries ?? []) : [];

  useEffect(() => {
    if (!addOpen && !importOpen && !exportOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (addOpen) { setAddOpen(false); setError(""); }
      if (importOpen && !importBusy) { setImportOpen(false); setError(""); }
      if (exportOpen && !exporting) { setExportOpen(false); setError(""); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [addOpen, importOpen, importBusy, exportOpen, exporting]);

  // Export: the download needs the bearer token, so it goes through
  // apiFetch as a blob rather than a plain <a href>.
  async function exportCorpus() {
    const selected = exportScope === "selected" ? [...exportSel] : null;
    if (selected && selected.length === 0) { setError("Pick at least one entry"); return; }
    setExporting(true);
    setError("");
    try {
      const qs = selected ? "?" + selected.map((k) => `entries=${encodeURIComponent(k)}`).join("&") : "";
      const resp = await apiFetch("/api/corpus/export" + qs);
      if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
      const url = URL.createObjectURL(await resp.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = downloadName(resp, "tpk-corpus.tar.gz");
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setExportOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setExporting(false);
    }
  }

  async function importCorpus() {
    if (!importFile) { setError("Choose a bundle archive first"); return; }
    setImportBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", importFile);
      body.append("replace", importReplace ? "true" : "false");
      if (importScope === "selected") {
        if (importSel.size === 0) throw new Error("Pick at least one entry");
        for (const k of importSel) body.append("entries", k);
      }
      const resp = await apiFetch("/api/corpus/import", { method: "POST", body });
      if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
      setImportOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImportBusy(false);
    }
  }

  async function refresh() {
    try {
      setRepos(await api("/api/repos"));
      setJobs(await api("/api/jobs"));
      setRuns((await api("/api/ingest-log?limit=50")).runs);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const activeJob = (key: string) =>
    jobs.find((j) => j.entry_key === key && (j.status === "queued" || j.status === "running"));

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); }
  }

  function addEntry(ingest: boolean) {
    if (!form.name.trim()) { setError("Name is required"); return; }
    act(async () => {
      await api("/api/repos", { ...form, ingest });
      setForm({ ...EMPTY_FORM });
      setAddOpen(false);
    });
  }

  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running");
  // Finished import jobs have no ingest-log row (the bundle carries its own
  // provenance), so they are shown from the job list until the server
  // restarts.
  const importResults = jobs.filter((j) => j.kind === "import" && (j.status === "ok" || j.status === "failed"));
  // History comes from the persistent log. A run the JobManager is driving
  // right now is shown in the live block above (with phase + progress), so it
  // is dropped from the history list until it has an end row.
  const liveKeys = new Set(activeJobs.map((j) => j.entry_key));
  const history = runs.filter((r) => !(r.status === "running" && liveKeys.has(r.entry_key)));

  return (
    <div className="tk-manage">
      {/* Add-flow errors render inside the modal; keep this page-level banner
          for the table's toggle/reindex/delete errors only. */}
      {!addOpen && !importOpen && !exportOpen && error && <div className="tk-manage-error">{error}</div>}

      <div className="tk-manage-header">
        <div>
          <div className="tk-manage-title">Corpus</div>
          <div className="tk-manage-subtitle">
            Versioned entries keyed name@ref — multiple releases coexist; the toggle gates searchability.
          </div>
        </div>
        <div className="tk-manage-header-spacer" />
        <div className="tk-manage-header-actions">
          {canTransfer && (
            <>
              <button type="button" className="tk-btn tk-btn-secondary" onClick={openExport}
                      disabled={exporting} title="Download the ingested graph + corpus registry as one .tar.gz">
                {exporting ? "Exporting…" : "Export corpus"}
              </button>
              <button type="button" className="tk-btn tk-btn-secondary" onClick={openImport}
                      disabled={activeJobs.length > 0}
                      title={activeJobs.length > 0 ? "Wait for the running job to finish" : "Load a bundle exported from another environment"}>
                Import corpus
              </button>
            </>
          )}
          {canManage && (
            <button type="button" className="tk-btn" onClick={openAdd}>
              Add corpus entry
            </button>
          )}
        </div>
      </div>

      <div className="tk-corpus-table-card">
        <table className="tk-corpus-table">
          <colgroup>
            <col /><col /><col /><col /><col /><col /><col />
          </colgroup>
          <thead>
            <tr>
              <th>Entry</th><th>Source</th><th>Mode</th><th>Nodes</th>
              <th>Last ingest</th><th>Enabled</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {repos.map((r) => {
              const job = activeJob(r.entry_key);
              const statusClass = job
                ? "tk-corpus-status-running"
                : r.last_status === "failed" ? "tk-corpus-status-failed"
                : r.last_status === "ok" ? "tk-corpus-status-ok"
                : "tk-corpus-status-never";
              return (
                <tr key={r.entry_key} className={r.enabled ? "" : "tk-corpus-row-disabled"}>
                  <td>
                    <div className="tk-corpus-entry-name">{r.entry_key}</div>
                    <div className="tk-corpus-entry-desc">{r.description}</div>
                  </td>
                  <td className="tk-corpus-source">{r.github ? `${r.github}@${r.ref}` : r.path}</td>
                  <td>
                    {r.extraction}
                    <div className="tk-corpus-mode-sub">{r.visibility}</div>
                  </td>
                  <td>{r.node_count}</td>
                  <td>
                    <span className={statusClass}>{job ? `${job.status}…` : (r.last_status ?? "never")}</span>
                    {job ? (
                      <div className="tk-corpus-ingest-sub">job {job.id}</div>
                    ) : r.last_sha ? (
                      <div className="tk-corpus-ingest-sub">{r.last_sha.slice(0, 12)}</div>
                    ) : null}
                  </td>
                  <td>
                    <button
                      type="button"
                      className={r.enabled ? "tk-toggle on" : "tk-toggle"}
                      role="switch"
                      aria-checked={r.enabled}
                      disabled={!canManage}
                      aria-label={`${r.enabled ? "Disable" : "Enable"} ${r.entry_key}`}
                      onClick={() => {
                        // nudge: one enabled ref per repo (issue #4)
                        const sibling = !r.enabled && repos.find(
                          (o) => o.name === r.name && o.ref !== r.ref && o.enabled);
                        if (sibling && !window.confirm(
                          `${sibling.entry_key} is already enabled. Enable ${r.entry_key} too? ` +
                          "(Both versions will appear in answers.)")) return;
                        act(() => api("/api/repos/toggle",
                          { name: r.name, ref: r.ref, enabled: !r.enabled }));
                      }}
                    />
                  </td>
                  <td>
                    {!canManage ? (
                      <span className="tk-corpus-entry-desc">view only</span>
                    ) : (
                    <div className="tk-corpus-actions">
                      <button
                        type="button"
                        className="tk-btn tk-btn-secondary"
                        disabled={!!job}
                        onClick={() => act(() => api("/api/repos/reindex", { name: r.name, ref: r.ref }))}
                      >
                        Reindex
                      </button>
                      {confirming === r.entry_key ? (
                        <span className="tk-corpus-confirm">
                          <label>
                            <input type="checkbox" checked={purge}
                                   onChange={(e) => setPurge(e.target.checked)} />
                            also delete indexed data
                          </label>
                          <button
                            type="button"
                            className="tk-btn tk-btn-solid-danger"
                            onClick={() => {
                              setConfirming(null);
                              act(() => api("/api/repos/delete", { name: r.name, ref: r.ref, purge }));
                            }}
                          >
                            Confirm
                          </button>
                          <button type="button" className="tk-btn tk-btn-secondary"
                                  onClick={() => setConfirming(null)}>
                            Cancel
                          </button>
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="tk-btn tk-btn-secondary tk-btn-danger"
                          onClick={() => { setConfirming(r.entry_key); setPurge(false); }}
                        >
                          Delete
                        </button>
                      )}
                    </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="tk-manage-lower">
        <div className="tk-jobs-panel">
          <div className="tk-jobs-panel-header">
            <div className="tk-jobs-panel-title">Ingest history</div>
            <div className="tk-jobs-panel-meta">every run — UI, CLI or kubectl · polled every 5s</div>
          </div>

          {activeJobs.length > 0 && (
            <div className="tk-jobs-active">
              {activeJobs.map((j) => (
                <div className="tk-job-active" key={j.id}>
                  <div className="tk-job-active-row">
                    <div className="tk-job-name">{j.entry_key}</div>
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
                  </div>
                  <div className="tk-job-detail">job {j.id} · started {formatStarted(j.submitted_at)}</div>
                </div>
              ))}
            </div>
          )}

          {history.length > 0 || importResults.length > 0 ? (
            <div className="tk-jobs-history">
              {importResults.map((j) => (
                <div className="tk-job-row" key={j.id} title={`job ${j.id}`}>
                  <div className="tk-job-name">{j.entry_key}{j.entries ? ` (${j.entries.length} ${j.entries.length === 1 ? "entry" : "entries"})` : ""}</div>
                  {j.status === "ok" ? (
                    <div className="tk-job-result">
                      ok · {Object.entries(j.streams ?? {}).map(([s, n]) => `${s} ${n}`).join(" · ")}
                      {j.entries ? ` · ${j.entries.join(", ")}` : ""}
                    </div>
                  ) : (
                    <div className="tk-job-result tk-job-result-failed">failed · {j.error || "unknown error"}</div>
                  )}
                  <div className="tk-job-time">{formatRelative(j.finished_at ?? j.submitted_at)}</div>
                </div>
              ))}
              {history.map((r) => (
                <div className="tk-job-row" key={r.run_id} title={`run ${r.run_id}${r.git_sha ? ` · ${r.git_sha}` : ""}`}>
                  <div className="tk-job-name">{r.entry_key}</div>
                  {r.status === "ok" ? (
                    <div className="tk-job-result">ok · {r.nodes} nodes · {r.edges} edges</div>
                  ) : r.status === "running" ? (
                    <div className="tk-job-result tk-job-status-running">running… (started outside the UI)</div>
                  ) : (
                    <div className="tk-job-result tk-job-result-failed">
                      {r.status} · {r.error || "unknown error"}
                    </div>
                  )}
                  <div className="tk-job-time">
                    {r.duration_s !== null && <span className="tk-job-duration">{durationLabel(r.duration_s)} · </span>}
                    {formatRelative(r.finished_at ?? r.started_at ?? "")}
                  </div>
                </div>
              ))}
            </div>
          ) : activeJobs.length === 0 && (
            <div className="tk-jobs-empty">No ingest runs yet.</div>
          )}
        </div>

      </div>

      {exportOpen && (
        <div className="tk-modal-overlay" onClick={closeExport}>
          <div className="tk-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tk-modal-header">
              <div className="tk-modal-title">Export corpus</div>
              <div className="tk-modal-spacer" />
              <button type="button" className="tk-modal-close" onClick={closeExport} aria-label="Close" disabled={exporting}>✕</button>
            </div>
            <form className="tk-modal-body" onSubmit={(e) => { e.preventDefault(); exportCorpus(); }}>
              <div className="tk-form-field">
                <label>Scope</label>
                <label className="tk-radio">
                  <input type="radio" name="export-scope" checked={exportScope === "all"} disabled={exporting}
                         onChange={() => setExportScope("all")} />
                  {" "}Whole corpus ({repos.length} {repos.length === 1 ? "entry" : "entries"})
                </label>
                <label className="tk-radio">
                  <input type="radio" name="export-scope" checked={exportScope === "selected"} disabled={exporting}
                         onChange={() => setExportScope("selected")} />
                  {" "}Selected entries
                </label>
                {exportScope === "selected" && (
                  <EntryPicker keys={repos.map((r) => r.entry_key)} selected={exportSel} onChange={setExportSel} disabled={exporting} />
                )}
                <div className="tk-form-hint">
                  One <code>.tar.gz</code> with the graph, corpus entries and ingest history of the chosen scope.
                  Users and roles are not included. <code>tpk import</code> and <strong>Import corpus</strong> read it.
                </div>
              </div>

              {error && <div className="tk-manage-error">{error}</div>}

              <div className="tk-modal-footer">
                <button type="button" className="tk-btn tk-btn-secondary" onClick={closeExport} disabled={exporting}>Cancel</button>
                <button type="submit" className="tk-btn"
                        disabled={exporting || (exportScope === "selected" && exportSel.size === 0)}>
                  {exporting ? "Exporting…" : "Export"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {importOpen && (
        <div className="tk-modal-overlay" onClick={closeImport}>
          <div className="tk-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tk-modal-header">
              <div className="tk-modal-title">Import corpus</div>
              <div className="tk-modal-spacer" />
              <button type="button" className="tk-modal-close" onClick={closeImport} aria-label="Close" disabled={importBusy}>✕</button>
            </div>
            <form className="tk-modal-body" onSubmit={(e) => { e.preventDefault(); importCorpus(); }}>
              <div className="tk-form-field">
                <label htmlFor="mng-bundle">Bundle archive <span className="tk-form-required">*</span></label>
                <input id="mng-bundle" type="file" accept=".tar.gz,.tgz,application/gzip" className="tk-input"
                       disabled={importBusy}
                       onChange={(e) => chooseImportFile(e.target.files?.[0] ?? null)} />
                <div className="tk-form-hint">
                  A <code>.tar.gz</code> from <strong>Export corpus</strong> or <code>tpk export</code>: graph, corpus
                  entries and ingest history. Users and roles are not included. Loading runs as a job in the
                  panel below; without <em>replace</em> it upserts into the current graph.
                </div>
                {importPeek === "reading" && <div className="tk-form-hint">Reading bundle…</div>}
                {importPeek === "ok" && importManifest && (
                  <div className="tk-form-hint" data-testid="bundle-summary">
                    Bundle{importManifest.partial ? " (partial export)" : ""}
                    {importManifest.created_at ? ` from ${new Date(importManifest.created_at).toLocaleString()}` : ""}:
                    {" "}{bundleEntries.length} {bundleEntries.length === 1 ? "entry" : "entries"}
                    {importManifest.streams?.kg_nodes ? `, ${importManifest.streams.kg_nodes.rows} nodes` : ""}.
                  </div>
                )}
                {importPeek === "none" && (
                  <div className="tk-form-hint">
                    Could not read an entry list from this archive (older bundle or browser); it will be imported whole.
                  </div>
                )}
              </div>
              {importPeek === "ok" && (
                <div className="tk-form-field">
                  <label>Scope</label>
                  <label className="tk-radio">
                    <input type="radio" name="import-scope" checked={importScope === "all"} disabled={importBusy}
                           onChange={() => setImportScope("all")} />
                    {" "}Whole bundle
                  </label>
                  <label className="tk-radio">
                    <input type="radio" name="import-scope" checked={importScope === "selected"} disabled={importBusy}
                           onChange={() => setImportScope("selected")} />
                    {" "}Selected entries
                  </label>
                  {importScope === "selected" && (
                    <EntryPicker keys={bundleEntries} selected={importSel} onChange={setImportSel} disabled={importBusy} />
                  )}
                </div>
              )}
              <div className="tk-form-field">
                <label>
                  <input type="checkbox" checked={importReplace} disabled={importBusy}
                         onChange={(e) => setImportReplace(e.target.checked)} />
                  {" "}{importScope === "selected" ? "Replace the selected entries" : "Replace existing graph"}
                </label>
                {importReplace && importScope === "selected" && (
                  <div className="tk-form-hint">
                    Deletes the selected entries' current nodes, edges and registry rows before loading, so stale
                    nodes from an earlier ingest do not linger. Other entries are untouched; ingest history is kept.
                  </div>
                )}
                {importReplace && importScope !== "selected" && (
                  <div className="tk-form-hint tk-form-warning" role="alert">
                    Drops and recreates every graph stream (nodes, edges, corpus entries, ingest history) before
                    loading — anything not in the bundle is gone. Roles that reference removed entries keep
                    dangling keys.
                  </div>
                )}
              </div>

              {error && <div className="tk-manage-error">{error}</div>}

              <div className="tk-modal-footer">
                <button type="button" className="tk-btn tk-btn-secondary" onClick={closeImport} disabled={importBusy}>Cancel</button>
                <button type="submit" className={importReplace && importScope !== "selected" ? "tk-btn tk-btn-solid-danger" : "tk-btn"}
                        disabled={importBusy || !importFile || importPeek === "reading" || (importScope === "selected" && importSel.size === 0)}>
                  {importBusy ? "Uploading…" : importReplace ? "Replace & import" : "Import"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {addOpen && (
        <div className="tk-modal-overlay" onClick={closeAdd}>
          <div className="tk-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tk-modal-header">
              <div className="tk-modal-title">Add corpus entry</div>
              <div className="tk-modal-spacer" />
              <button type="button" className="tk-modal-close" onClick={closeAdd} aria-label="Close">✕</button>
            </div>
            <form className="tk-modal-body" onSubmit={(e) => { e.preventDefault(); addEntry(true); }}>
              <div className="tk-form-field">
                <label htmlFor="mng-name">Name <span className="tk-form-required">*</span></label>
                <input id="mng-name" ref={nameInputRef} className="tk-input" placeholder="e.g. proton" required autoFocus
                       value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>

              <div className="tk-form-row">
                <div className="tk-form-field">
                  <label htmlFor="mng-github">GitHub org/repo</label>
                  <input id="mng-github" className="tk-input" placeholder="org/repo" value={form.github}
                         onChange={(e) => setForm({ ...form, github: e.target.value })} />
                </div>
                <div className="tk-form-field tk-form-field-narrow">
                  <label htmlFor="mng-ref">Ref</label>
                  <input id="mng-ref" className="tk-input" placeholder="v1.0.0" value={form.ref}
                         onChange={(e) => setForm({ ...form, ref: e.target.value })} />
                </div>
              </div>

              <div className="tk-form-field">
                <label htmlFor="mng-path">Local path (dev mode — alternative to GitHub)</label>
                <input id="mng-path" className="tk-input" placeholder="/path/to/repo" value={form.path}
                       onChange={(e) => setForm({ ...form, path: e.target.value })} />
              </div>

              <div className="tk-form-row">
                <div className="tk-form-field">
                  <label htmlFor="mng-visibility">Visibility</label>
                  <select id="mng-visibility" className="tk-select" value={form.visibility}
                          onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
                    <option value="internal">internal</option>
                    <option value="public">public</option>
                  </select>
                  {form.visibility === "public" && (
                    <div className="tk-form-hint tk-form-warning" role="alert">
                      Public entries are visible to unauthenticated users whenever anonymous access is
                      enabled — and this cannot be changed after the entry is created.
                    </div>
                  )}
                </div>
                <div className="tk-form-field">
                  <label htmlFor="mng-extraction">Extraction</label>
                  <select id="mng-extraction" className="tk-select" value={form.extraction}
                          onChange={(e) => setForm({ ...form, extraction: e.target.value })}>
                    <option value="code-only">code-only</option>
                    <option value="semantic">semantic</option>
                  </select>
                </div>
              </div>
              <div className="tk-form-hint">code-only is free and offline; semantic adds an LLM pass over docs/YAML.</div>

              <div className="tk-form-field">
                <label htmlFor="mng-description">Description</label>
                <input id="mng-description" className="tk-input" placeholder="optional" value={form.description}
                       onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </div>

              {error && <div className="tk-manage-error">{error}</div>}

              <div className="tk-modal-footer">
                <button type="button" className="tk-btn tk-btn-secondary" onClick={closeAdd}>Cancel</button>
                <button type="button" className="tk-btn tk-btn-secondary" onClick={() => addEntry(false)}>Add only</button>
                <button type="submit" className="tk-btn">Add &amp; index</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
