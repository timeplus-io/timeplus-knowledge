import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";

type Repo = {
  name: string; ref: string; entry_key: string; github: string; path: string;
  enabled: boolean; visibility: string; extraction: string; description: string;
  node_count: number; last_status: string | null; last_sha: string | null;
  last_time: string | null;
};
type Job = {
  id: string; entry_key: string; status: string; nodes: number; edges: number;
  error: string | null; submitted_at: string; finished_at: string | null;
};

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

export default function Manage() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [purge, setPurge] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);

  function openAdd() { setForm({ ...EMPTY_FORM }); setError(""); setAddOpen(true); }
  function closeAdd() { setAddOpen(false); setError(""); }

  useEffect(() => {
    if (!addOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setAddOpen(false); setError(""); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [addOpen]);

  async function refresh() {
    try {
      setRepos(await api("/api/repos"));
      setJobs(await api("/api/jobs"));
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
  const finishedJobs = jobs
    .filter((j) => j.status === "ok" || j.status === "failed")
    .sort((a, b) => (b.finished_at ?? "").localeCompare(a.finished_at ?? ""));

  return (
    <div className="tk-manage">
      {/* Add-flow errors render inside the modal; keep this page-level banner
          for the table's toggle/reindex/delete errors only. */}
      {!addOpen && error && <div className="tk-manage-error">{error}</div>}

      <div className="tk-manage-header">
        <div>
          <div className="tk-manage-title">Corpus</div>
          <div className="tk-manage-subtitle">
            Versioned entries keyed name@ref — multiple releases coexist; the toggle gates searchability.
          </div>
        </div>
        <div className="tk-manage-header-spacer" />
        <button type="button" className="tk-btn" onClick={openAdd}>
          Add corpus entry
        </button>
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
            <div className="tk-jobs-panel-title">Ingest jobs</div>
            <div className="tk-jobs-panel-meta">polled every 5s</div>
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
                        <div className="tk-job-status-running">running…</div>
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

          {finishedJobs.length > 0 ? (
            <div className="tk-jobs-history">
              {finishedJobs.map((j) => (
                <div className="tk-job-row" key={j.id}>
                  <div className="tk-job-name">{j.entry_key}</div>
                  {j.status === "ok" ? (
                    <div className="tk-job-result">ok · {j.nodes} nodes · {j.edges} edges</div>
                  ) : (
                    <div className="tk-job-result tk-job-result-failed">failed · {j.error ?? "unknown error"}</div>
                  )}
                  <div className="tk-job-time">{j.finished_at ? formatRelative(j.finished_at) : ""}</div>
                </div>
              ))}
            </div>
          ) : activeJobs.length === 0 && (
            <div className="tk-jobs-empty">No ingest jobs yet.</div>
          )}
        </div>

      </div>

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
