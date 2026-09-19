import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Token = {
  token_id: string; username: string; name: string; hint: string;
  created_at: string; expires_at: string | null; last_used_at: string | null;
};
type Created = Token & { token: string };

const EXPIRY_OPTIONS: { label: string; days: number | null }[] = [
  { label: "Never", days: null },
  { label: "30 days", days: 30 },
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
];

async function call(path: string, body?: unknown): Promise<any> {
  const resp = await apiFetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON body */ }
  if (!resp.ok) {
    const detail = data && typeof data === "object" ? data.detail : null;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${resp.status}`);
  }
  return data;
}

const fmt = (iso: string | null, empty: string) =>
  iso ? new Date(iso).toLocaleString() : empty;

export function mcpCommand(origin: string, token: string): string {
  return `claude mcp add --transport http timeplus-knowledge ${origin}/mcp --header "Authorization: Bearer ${token}"`;
}

export default function Tokens() {
  const [tokens, setTokens] = useState<Token[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [days, setDays] = useState<number | null>(null);
  const [created, setCreated] = useState<Created | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [copied, setCopied] = useState("");

  const refresh = useCallback(async () => {
    try { setTokens((await call("/api/tokens")).tokens); setError(""); }
    catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  async function create() {
    try {
      const body: Record<string, unknown> = { name: name.trim() };
      if (days !== null) body.expires_days = days;
      setCreated(await call("/api/tokens", body));
      setShowCreate(false); setName(""); setDays(null);
      refresh();
    } catch (e) { setError((e as Error).message); }
  }

  async function revoke(token_id: string) {
    try { await call("/api/tokens/revoke", { token_id }); setConfirming(null); refresh(); }
    catch (e) { setError((e as Error).message); }
  }

  async function copy(label: string, text: string) {
    await navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(""), 1500);
  }

  // Dismissing the panel drops the only copy of the plaintext.
  const closeCreated = () => setCreated(null);

  return (
    <div className="tk-users">
      {error && <div className="tk-manage-error" role="alert">{error}</div>}
      <div className="tk-manage-header">
        <div>
          <div className="tk-manage-title">API tokens</div>
          <div className="tk-manage-subtitle">
            Connect a coding agent (Claude Code, Cursor) to the knowledge graph over MCP.
            A token acts as you: same corpus access, same permissions.
          </div>
        </div>
        <div className="tk-manage-header-spacer" />
        <button type="button" className="tk-btn" onClick={() => setShowCreate(true)}>New token</button>
      </div>

      {created && (
        <div className="tk-token-created" role="status">
          <strong>Copy this token now — it will not be shown again.</strong>
          <pre className="tk-token-value">{created.token}</pre>
          <button type="button" className="tk-btn tk-btn-secondary"
                  onClick={() => copy("token", created.token)}>
            {copied === "token" ? "Copied" : "Copy token"}
          </button>
          <div className="tk-manage-subtitle">Add it to Claude Code:</div>
          <pre className="tk-token-value">{mcpCommand(window.location.origin, created.token)}</pre>
          <button type="button" className="tk-btn tk-btn-secondary"
                  onClick={() => copy("cmd", mcpCommand(window.location.origin, created.token))}>
            {copied === "cmd" ? "Copied" : "Copy command"}
          </button>
          <button type="button" className="tk-btn" onClick={closeCreated}>Done</button>
        </div>
      )}

      <div className="tk-users-table-card">
      <table className="tk-users-table">
        <thead>
          <tr><th>Name</th><th>Token</th><th>Created</th><th>Expires</th><th>Last used</th><th /></tr>
        </thead>
        <tbody>
          {tokens.length === 0 && (
            <tr><td colSpan={6}>No tokens yet.</td></tr>
          )}
          {tokens.map((t) => (
            <tr key={t.token_id}>
              <td>{t.name}</td>
              <td><code>tpk_…{t.hint}</code></td>
              <td>{fmt(t.created_at, "")}</td>
              <td>{fmt(t.expires_at, "Never")}</td>
              <td>{fmt(t.last_used_at, "Never")}</td>
              <td className="tk-users-actions">
                {confirming === t.token_id ? (
                  <>
                    <button type="button" className="tk-btn tk-btn-danger" onClick={() => revoke(t.token_id)}>Confirm revoke</button>
                    <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setConfirming(null)}>Cancel</button>
                  </>
                ) : (
                  <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setConfirming(t.token_id)}>Revoke</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      {showCreate && (
        <div className="tk-modal-overlay" onClick={() => setShowCreate(false)}>
          <div className="tk-modal" role="dialog" aria-modal="true" aria-labelledby="new-token-title"
               onClick={(e) => e.stopPropagation()}>
            <div className="tk-modal-header">
              <div id="new-token-title" className="tk-modal-title">New API token</div>
              <div className="tk-modal-spacer" />
              <button type="button" className="tk-modal-close" aria-label="Close"
                      onClick={() => setShowCreate(false)}>&times;</button>
            </div>
            <form onSubmit={(e) => { e.preventDefault(); create(); }}>
              <div className="tk-modal-body">
                <div className="tk-form-field">
                  <label htmlFor="tok-name">
                    Name <span className="tk-form-required">*</span>
                  </label>
                  <input id="tok-name" className="tk-input" value={name} maxLength={64} required autoFocus
                         placeholder="e.g. laptop — Claude Code"
                         onChange={(e) => setName(e.target.value)} />
                </div>
                <div className="tk-form-field">
                  <label htmlFor="tok-expiry">Expires</label>
                  <select id="tok-expiry" className="tk-input" value={days ?? ""}
                          onChange={(e) => setDays(e.target.value ? Number(e.target.value) : null)}>
                    {EXPIRY_OPTIONS.map((o) => (
                      <option key={o.label} value={o.days ?? ""}>{o.label}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="tk-modal-footer">
                <button type="button" className="tk-btn tk-btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
                <button type="submit" className="tk-btn" disabled={!name.trim()}>Create</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
