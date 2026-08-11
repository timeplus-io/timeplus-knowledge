import { useEffect, useState } from "react";

type Repo = {
  name: string; ref: string; entry_key: string; github: string; path: string;
  enabled: boolean; visibility: string; extraction: string; description: string;
  node_count: number; last_status: string | null; last_sha: string | null;
  last_time: string | null;
};
type Job = { id: string; entry_key: string; status: string; nodes: number;
  edges: number; error: string | null };

const EMPTY_FORM = { name: "", github: "", ref: "", path: "", visibility: "internal",
  extraction: "code-only", description: "" };

async function api(path: string, token: string, body?: unknown) {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["X-Admin-Token"] = token;
  const resp = await fetch(path, body === undefined ? { headers } : {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    if (resp.status === 401) throw new Error("Unauthorized — set the admin token");
    throw new Error(`${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

export default function Manage() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [purge, setPurge] = useState(false);
  const [token, setToken] = useState(() => sessionStorage.getItem("tpk_admin_token") ?? "");

  function updateToken(value: string) {
    setToken(value);
    sessionStorage.setItem("tpk_admin_token", value);
  }

  async function refresh() {
    try {
      setRepos(await api("/api/repos", token));
      setJobs(await api("/api/jobs", token));
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const activeJob = (key: string) =>
    jobs.find((j) => j.entry_key === key && (j.status === "queued" || j.status === "running"));

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); }
  }

  return (
    <main className="manage">
      <div className="toolbar">
        <input type="password" placeholder="Admin token (if required)" value={token}
               onChange={(e) => updateToken(e.target.value)} />
      </div>
      {error && <div className="manage-error">{error}</div>}
      <table className="repo-table">
        <thead>
          <tr><th>Entry</th><th>Source</th><th>Mode</th><th>Nodes</th>
              <th>Last ingest</th><th>Enabled</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {repos.map((r) => (
            <tr key={r.entry_key} className={r.enabled ? "" : "disabled-row"}>
              <td><strong>{r.entry_key}</strong><div className="muted">{r.description}</div></td>
              <td>{r.github ? `${r.github}@${r.ref}` : r.path}</td>
              <td>{r.extraction}<div className="muted">{r.visibility}</div></td>
              <td>{r.node_count}</td>
              <td>{activeJob(r.entry_key)
                ? <span className="running">{activeJob(r.entry_key)!.status}…</span>
                : <>{r.last_status ?? "never"}<div className="muted">{r.last_sha?.slice(0, 12)}</div></>}
              </td>
              <td>
                <button className="secondary"
                        onClick={() => {
                          // nudge: one enabled ref per repo (issue #4)
                          const sibling = !r.enabled && repos.find(
                            (o) => o.name === r.name && o.ref !== r.ref && o.enabled);
                          if (sibling && !window.confirm(
                            `${sibling.entry_key} is already enabled. Enable ${r.entry_key} too? ` +
                            "(Both versions will appear in answers.)")) return;
                          act(() => api("/api/repos/toggle", token,
                            { name: r.name, ref: r.ref, enabled: !r.enabled }));
                        }}>
                  {r.enabled ? "Disable" : "Enable"}
                </button>
              </td>
              <td className="actions">
                <button className="secondary" disabled={!!activeJob(r.entry_key)}
                        onClick={() => act(() => api("/api/repos/reindex", token,
                          { name: r.name, ref: r.ref }))}>Reindex</button>
                {confirming === r.entry_key ? (
                  <span className="confirm">
                    <label><input type="checkbox" checked={purge}
                                  onChange={(e) => setPurge(e.target.checked)} />
                      also delete indexed data</label>
                    <button className="danger"
                            onClick={() => { setConfirming(null);
                              act(() => api("/api/repos/delete", token,
                                { name: r.name, ref: r.ref, purge })); }}>Confirm</button>
                    <button className="secondary"
                            onClick={() => setConfirming(null)}>Cancel</button>
                  </span>
                ) : (
                  <button className="danger"
                          onClick={() => { setConfirming(r.entry_key); setPurge(false); }}>
                    Delete</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Add corpus entry</h2>
      <form className="add-form" onSubmit={(e) => { e.preventDefault();
        act(async () => { await api("/api/repos", token, { ...form, ingest: true });
          setForm({ ...EMPTY_FORM }); }); }}>
        <input placeholder="name" required value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input placeholder="github org/repo (or leave empty for local path)"
               value={form.github}
               onChange={(e) => setForm({ ...form, github: e.target.value })} />
        <input placeholder="ref (tag/branch, required with github)" value={form.ref}
               onChange={(e) => setForm({ ...form, ref: e.target.value })} />
        <input placeholder="local path (dev mode)" value={form.path}
               onChange={(e) => setForm({ ...form, path: e.target.value })} />
        <select value={form.visibility}
                onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
          <option value="internal">internal</option>
          <option value="public">public</option>
        </select>
        <select value={form.extraction}
                onChange={(e) => setForm({ ...form, extraction: e.target.value })}>
          <option value="code-only">code-only</option>
          <option value="semantic">semantic</option>
        </select>
        <input placeholder="description" value={form.description}
               onChange={(e) => setForm({ ...form, description: e.target.value })} />
        <button type="submit">Add &amp; index</button>
      </form>
    </main>
  );
}
