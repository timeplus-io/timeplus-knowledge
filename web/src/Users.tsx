import { useEffect, useState } from "react";
import { apiFetch } from "./api";

type ApiUser = { username: string; role: string; must_change_password: boolean; disabled: boolean };
type ApiRole = { name: string; entry_keys: string[]; description: string };
type Repo = { entry_key: string };

const EMPTY_USER = { username: "", password: "", role: "admin", must_change_password: true };
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

  const [newUser, setNewUser] = useState({ ...EMPTY_USER });
  const [confirmingUser, setConfirmingUser] = useState<string | null>(null);
  const [resettingUser, setResettingUser] = useState<string | null>(null);
  const [resetPassword, setResetPassword] = useState("");

  const [roleEdits, setRoleEdits] =
    useState<Record<string, { entry_keys: string[]; description: string }>>({});
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

  return (
    <main className="manage">
      {error && <div className="manage-error">{error}</div>}

      <h2>Users</h2>
      <table className="repo-table">
        <thead>
          <tr><th>Username</th><th>Role</th><th>Status</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.username} className={u.disabled ? "disabled-row" : ""}>
              <td>
                <strong>{u.username}</strong>
                {u.must_change_password && <div className="muted">must change password</div>}
              </td>
              <td>
                <select value={u.role}
                        onChange={(e) => act(() => call("/api/users/update",
                          { username: u.username, role: e.target.value }))}>
                  <option value="admin">admin</option>
                  {roles.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
                </select>
              </td>
              <td>{u.disabled ? "disabled" : "active"}</td>
              <td className="actions">
                <button className="secondary"
                        onClick={() => act(() => call("/api/users/update",
                          { username: u.username, disabled: !u.disabled }))}>
                  {u.disabled ? "Enable" : "Disable"}
                </button>
                {resettingUser === u.username ? (
                  <span className="confirm">
                    <input type="password" placeholder="new password" value={resetPassword}
                           onChange={(e) => setResetPassword(e.target.value)} />
                    <button className="secondary"
                            onClick={() => {
                              const password = resetPassword;
                              setResettingUser(null);
                              setResetPassword("");
                              act(() => call("/api/users/update", { username: u.username, password }));
                            }}>
                      Save
                    </button>
                    <button className="secondary"
                            onClick={() => { setResettingUser(null); setResetPassword(""); }}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button className="secondary"
                          onClick={() => { setResettingUser(u.username); setResetPassword(""); }}>
                    Reset password
                  </button>
                )}
                {confirmingUser === u.username ? (
                  <span className="confirm">
                    <button className="danger"
                            onClick={() => {
                              setConfirmingUser(null);
                              act(() => call("/api/users/delete", { username: u.username }));
                            }}>
                      Confirm
                    </button>
                    <button className="secondary" onClick={() => setConfirmingUser(null)}>Cancel</button>
                  </span>
                ) : (
                  <button className="danger" onClick={() => setConfirmingUser(u.username)}>Delete</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Add user</h2>
      <form className="add-form" onSubmit={(e) => {
        e.preventDefault();
        act(async () => {
          await call("/api/users", { ...newUser });
          setNewUser({ ...EMPTY_USER });
        });
      }}>
        <input placeholder="username" required value={newUser.username}
               onChange={(e) => setNewUser({ ...newUser, username: e.target.value })} />
        <input placeholder="initial password" type="password" required value={newUser.password}
               onChange={(e) => setNewUser({ ...newUser, password: e.target.value })} />
        <select value={newUser.role} onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}>
          <option value="admin">admin</option>
          {roles.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
        </select>
        <label className="checkbox-field">
          <input type="checkbox" checked={newUser.must_change_password}
                 onChange={(e) => setNewUser({ ...newUser, must_change_password: e.target.checked })} />
          must change password
        </label>
        <button type="submit">Add user</button>
      </form>

      <h2>Roles</h2>
      <table className="repo-table">
        <thead>
          <tr><th>Name</th><th>Entry keys</th><th>Description</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {roles.map((r) => {
            const edit = roleEdits[r.name] ?? { entry_keys: r.entry_keys, description: r.description };
            return (
              <tr key={r.name}>
                <td><strong>{r.name}</strong></td>
                <td>
                  <div className="entry-keys">
                    {entryKeys.map((k) => (
                      <label key={k} className="checkbox-field">
                        <input type="checkbox" checked={edit.entry_keys.includes(k)}
                               onChange={() => toggleEntryKey(r.name, k)} />
                        {k}
                      </label>
                    ))}
                    {entryKeys.length === 0 && <span className="muted">no corpus entries yet</span>}
                  </div>
                </td>
                <td>
                  <input value={edit.description}
                         onChange={(e) => setRoleEdits((prev) => ({
                           ...prev, [r.name]: { ...edit, description: e.target.value },
                         }))} />
                </td>
                <td className="actions">
                  <button className="secondary"
                          onClick={() => act(() => call("/api/roles",
                            { name: r.name, entry_keys: edit.entry_keys, description: edit.description }))}>
                    Save
                  </button>
                  {confirmingRole === r.name ? (
                    <span className="confirm">
                      <button className="danger"
                              onClick={() => {
                                setConfirmingRole(null);
                                act(() => call("/api/roles/delete", { name: r.name }));
                              }}>
                        Confirm
                      </button>
                      <button className="secondary" onClick={() => setConfirmingRole(null)}>Cancel</button>
                    </span>
                  ) : (
                    <button className="danger" onClick={() => setConfirmingRole(r.name)}>Delete</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h2>Add role</h2>
      <form className="add-form" onSubmit={(e) => {
        e.preventDefault();
        act(async () => {
          await call("/api/roles", { ...newRole });
          setNewRole({ ...EMPTY_ROLE });
        });
      }}>
        <input placeholder="name" required value={newRole.name}
               onChange={(e) => setNewRole({ ...newRole, name: e.target.value })} />
        <div className="entry-keys">
          {entryKeys.map((k) => (
            <label key={k} className="checkbox-field">
              <input type="checkbox" checked={newRole.entry_keys.includes(k)}
                     onChange={() => setNewRole((prev) => ({
                       ...prev,
                       entry_keys: prev.entry_keys.includes(k)
                         ? prev.entry_keys.filter((x) => x !== k)
                         : [...prev.entry_keys, k],
                     }))} />
              {k}
            </label>
          ))}
          {entryKeys.length === 0 && <span className="muted">no corpus entries yet</span>}
        </div>
        <input placeholder="description" value={newRole.description}
               onChange={(e) => setNewRole({ ...newRole, description: e.target.value })} />
        <button type="submit">Add role</button>
      </form>
    </main>
  );
}
