import { useEffect, useState } from "react";
import { apiFetch } from "./api";

type ApiUser = { username: string; role: string; must_change_password: boolean; disabled: boolean };
type ApiRole = { name: string; entry_keys: string[]; description: string };
type Repo = { entry_key: string };

// Least-privilege: force an explicit role pick rather than defaulting new
// users to admin.
const EMPTY_USER = { username: "", password: "", role: "", must_change_password: true };
const EMPTY_ROLE = { name: "", entry_keys: [] as string[], description: "" };

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

export default function Users() {
  const [users, setUsers] = useState<ApiUser[]>([]);
  const [roles, setRoles] = useState<ApiRole[]>([]);
  const [entryKeys, setEntryKeys] = useState<string[]>([]);
  const [error, setError] = useState("");

  const [showAddUser, setShowAddUser] = useState(false);
  const [newUser, setNewUser] = useState({ ...EMPTY_USER });
  const [confirmingUser, setConfirmingUser] = useState<string | null>(null);
  const [resettingUser, setResettingUser] = useState<string | null>(null);
  const [resetPassword, setResetPassword] = useState("");

  const [roleEdits, setRoleEdits] =
    useState<Record<string, { entry_keys: string[]; description: string }>>({});
  const [showAddRole, setShowAddRole] = useState(false);
  const [newRole, setNewRole] = useState({ ...EMPTY_ROLE });
  const [confirmingRole, setConfirmingRole] = useState<string | null>(null);

  async function refresh() {
    try {
      const [u, r, repos]: [ApiUser[], ApiRole[], Repo[]] = await Promise.all([
        call("/api/users"),
        call("/api/roles"),
        call("/api/repos"),
      ]);
      setUsers(u);
      setRoles(r);
      setEntryKeys([...new Set(repos.map((x) => x.entry_key))].sort());
      setRoleEdits(Object.fromEntries(
        r.map((role) => [role.name, { entry_keys: role.entry_keys, description: role.description }])
      ));
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { refresh(); }, []);

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); }
  }

  function toggleEntryKey(name: string, key: string) {
    setRoleEdits((prev) => {
      const cur = prev[name] ?? { entry_keys: [], description: "" };
      const has = cur.entry_keys.includes(key);
      return {
        ...prev,
        [name]: { ...cur, entry_keys: has ? cur.entry_keys.filter((k) => k !== key) : [...cur.entry_keys, key] },
      };
    });
  }

  function memberCount(roleName: string): number {
    return users.filter((u) => u.role === roleName).length;
  }

  function corpusAccessGrid(
    keys: string[],
    onToggle: (key: string) => void,
    idPrefix: string,
  ) {
    if (entryKeys.length === 0) {
      return <span className="tk-corpus-chip-empty">no corpus entries yet</span>;
    }
    return entryKeys.map((k) => {
      const checked = keys.includes(k);
      return (
        <button
          type="button"
          key={`${idPrefix}-${k}`}
          className="tk-corpus-chip"
          aria-pressed={checked}
          onClick={() => onToggle(k)}
        >
          <span className={checked ? "tk-corpus-chip-box checked" : "tk-corpus-chip-box"}>
            {checked ? "✓" : ""}
          </span>
          <span className="tk-corpus-chip-key">{k}</span>
        </button>
      );
    });
  }

  return (
    <div className="tk-users">
      {error && <div className="tk-manage-error">{error}</div>}

      <div className="tk-manage-header">
        <div>
          <div className="tk-manage-title">Users &amp; roles</div>
          <div className="tk-manage-subtitle">
            A role scopes chat and search to the corpus entries it lists; admin is unscoped.
          </div>
        </div>
        <div className="tk-manage-header-spacer" />
        <button
          type="button"
          className="tk-btn tk-btn-secondary"
          onClick={() => setShowAddRole((v) => !v)}
        >
          Add role
        </button>
        <button
          type="button"
          className="tk-btn"
          onClick={() => setShowAddUser((v) => !v)}
        >
          Add user
        </button>
      </div>

      {showAddUser && (
        <div className="tk-add-form-card">
          <div className="tk-add-form-title">Add user</div>
          <form
            className="tk-add-form-fields"
            onSubmit={(e) => {
              e.preventDefault();
              act(async () => {
                await call("/api/users", { ...newUser });
                setNewUser({ ...EMPTY_USER });
                setShowAddUser(false);
              });
            }}
          >
            <div className="tk-form-row">
              <div className="tk-form-field">
                <label htmlFor="usr-username">Username <span className="tk-form-required">*</span></label>
                <input id="usr-username" className="tk-input" placeholder="username" required
                       value={newUser.username}
                       onChange={(e) => setNewUser({ ...newUser, username: e.target.value })} />
              </div>
              <div className="tk-form-field">
                <label htmlFor="usr-password">Initial password <span className="tk-form-required">*</span></label>
                <input id="usr-password" className="tk-input" type="password" placeholder="initial password"
                       required value={newUser.password}
                       onChange={(e) => setNewUser({ ...newUser, password: e.target.value })} />
              </div>
            </div>
            <div className="tk-form-field">
              <label htmlFor="usr-role">Role <span className="tk-form-required">*</span></label>
              <select id="usr-role" className="tk-select" required value={newUser.role}
                      onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}>
                <option value="" disabled>select role…</option>
                <option value="admin">admin</option>
                {roles.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
              </select>
            </div>
            <div className="tk-form-field">
              <label htmlFor="usr-must-change" className="checkbox-field">
                <input id="usr-must-change" type="checkbox" checked={newUser.must_change_password}
                       onChange={(e) => setNewUser({ ...newUser, must_change_password: e.target.checked })} />
                must change password
              </label>
            </div>
            <div className="tk-form-actions">
              <button type="button" className="tk-btn tk-btn-secondary"
                      onClick={() => { setNewUser({ ...EMPTY_USER }); setShowAddUser(false); }}>
                Cancel
              </button>
              <button type="submit" className="tk-btn" disabled={!newUser.role}>Add user</button>
            </div>
          </form>
        </div>
      )}

      <div className="tk-users-table-card">
        <table className="tk-users-table">
          <colgroup>
            <col /><col /><col /><col />
          </colgroup>
          <thead>
            <tr><th>Username</th><th>Role</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username} className={u.disabled ? "tk-users-row-disabled" : ""}>
                <td>
                  <div className="tk-user-identity">
                    <div className="tk-avatar">{u.username.slice(0, 1).toUpperCase()}</div>
                    <div>
                      <div className="tk-user-name">{u.username}</div>
                      {u.must_change_password && <div className="tk-user-sub">must change password</div>}
                    </div>
                  </div>
                </td>
                <td>
                  {u.role === "admin" ? (
                    <span className="tk-user-role-admin">admin &middot; unscoped</span>
                  ) : (
                    <select className="tk-select tk-user-role-select" value={u.role}
                            onChange={(e) => act(() => call("/api/users/update",
                              { username: u.username, role: e.target.value }))}>
                      <option value="admin">admin</option>
                      {roles.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
                    </select>
                  )}
                </td>
                <td>{u.disabled ? "disabled" : "active"}</td>
                <td>
                  <div className="tk-users-actions">
                    {resettingUser === u.username ? (
                      <span className="tk-users-confirm">
                        <input className="tk-inline-input" type="password" placeholder="new password"
                               value={resetPassword}
                               onChange={(e) => setResetPassword(e.target.value)} />
                        <button type="button" className="tk-btn tk-btn-secondary"
                                onClick={() => {
                                  const password = resetPassword;
                                  setResettingUser(null);
                                  setResetPassword("");
                                  act(() => call("/api/users/update", { username: u.username, password }));
                                }}>
                          Save
                        </button>
                        <button type="button" className="tk-btn tk-btn-secondary"
                                onClick={() => { setResettingUser(null); setResetPassword(""); }}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button type="button" className="tk-btn tk-btn-secondary"
                              onClick={() => { setResettingUser(u.username); setResetPassword(""); }}>
                        Reset password
                      </button>
                    )}
                    {u.role !== "admin" && (
                      <>
                        <button type="button" className="tk-btn tk-btn-secondary"
                                onClick={() => act(() => call("/api/users/update",
                                  { username: u.username, disabled: !u.disabled }))}>
                          {u.disabled ? "Enable" : "Disable"}
                        </button>
                        {confirmingUser === u.username ? (
                          <span className="tk-users-confirm">
                            <button type="button" className="tk-btn tk-btn-solid-danger"
                                    onClick={() => {
                                      setConfirmingUser(null);
                                      act(() => call("/api/users/delete", { username: u.username }));
                                    }}>
                              Confirm
                            </button>
                            <button type="button" className="tk-btn tk-btn-secondary"
                                    onClick={() => setConfirmingUser(null)}>
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button type="button" className="tk-btn tk-btn-secondary tk-btn-danger"
                                  onClick={() => setConfirmingUser(u.username)}>
                            Delete
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showAddRole && (
        <div className="tk-add-form-card">
          <div className="tk-add-form-title">Add role</div>
          <form
            className="tk-add-form-fields"
            onSubmit={(e) => {
              e.preventDefault();
              act(async () => {
                await call("/api/roles", { ...newRole });
                setNewRole({ ...EMPTY_ROLE });
                setShowAddRole(false);
              });
            }}
          >
            <div className="tk-form-field">
              <label htmlFor="role-name">Name <span className="tk-form-required">*</span></label>
              <input id="role-name" className="tk-input" placeholder="name" required value={newRole.name}
                     onChange={(e) => setNewRole({ ...newRole, name: e.target.value })} />
            </div>
            <div className="tk-form-field">
              <span className="tk-corpus-access-label">Corpus access</span>
              <div className="tk-corpus-access-grid">
                {corpusAccessGrid(
                  newRole.entry_keys,
                  (k) => setNewRole((prev) => ({
                    ...prev,
                    entry_keys: prev.entry_keys.includes(k)
                      ? prev.entry_keys.filter((x) => x !== k)
                      : [...prev.entry_keys, k],
                  })),
                  "new-role",
                )}
              </div>
            </div>
            <div className="tk-form-field">
              <label htmlFor="role-desc">Description</label>
              <input id="role-desc" className="tk-input" placeholder="description" value={newRole.description}
                     onChange={(e) => setNewRole({ ...newRole, description: e.target.value })} />
            </div>
            <div className="tk-form-actions">
              <button type="button" className="tk-btn tk-btn-secondary"
                      onClick={() => { setNewRole({ ...EMPTY_ROLE }); setShowAddRole(false); }}>
                Cancel
              </button>
              <button type="submit" className="tk-btn">Add role</button>
            </div>
          </form>
        </div>
      )}

      <div className="tk-role-cards">
        {roles.map((r) => {
          const edit = roleEdits[r.name] ?? { entry_keys: r.entry_keys, description: r.description };
          const count = memberCount(r.name);
          return (
            <div className="tk-role-card" key={r.name}>
              <div className="tk-role-card-header">
                <div className="tk-role-card-name">{r.name}</div>
                <div className="tk-role-card-meta">
                  {count} member{count === 1 ? "" : "s"} &middot; {r.description || "no description"}
                </div>
                <div className="tk-role-card-header-spacer" />
                <button type="button" className="tk-btn tk-btn-secondary"
                        onClick={() => act(() => call("/api/roles",
                          { name: r.name, entry_keys: edit.entry_keys, description: edit.description }))}>
                  Save
                </button>
                {confirmingRole === r.name ? (
                  <span className="tk-users-confirm">
                    <button type="button" className="tk-btn tk-btn-solid-danger"
                            onClick={() => {
                              setConfirmingRole(null);
                              act(() => call("/api/roles/delete", { name: r.name }));
                            }}>
                      Confirm
                    </button>
                    <button type="button" className="tk-btn tk-btn-secondary"
                            onClick={() => setConfirmingRole(null)}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button type="button" className="tk-btn tk-btn-secondary tk-btn-danger"
                          onClick={() => setConfirmingRole(r.name)}>
                    Delete
                  </button>
                )}
              </div>

              <span className="tk-corpus-access-label">Corpus access</span>
              <div className="tk-corpus-access-grid">
                {corpusAccessGrid(edit.entry_keys, (k) => toggleEntryKey(r.name, k), r.name)}
              </div>

              <div className="tk-form-field">
                <label htmlFor={`role-desc-${r.name}`}>Description</label>
                <input id={`role-desc-${r.name}`} className="tk-input" value={edit.description}
                       onChange={(e) => setRoleEdits((prev) => ({
                         ...prev, [r.name]: { ...edit, description: e.target.value },
                       }))} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
