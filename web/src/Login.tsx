import { useState } from "react";
import { apiFetch, setToken } from "./api";

type Me = { username: string; role: string };

async function fetchMe(): Promise<Me> {
  const resp = await apiFetch("/auth/me");
  if (!resp.ok) throw new Error(`could not load profile (HTTP ${resp.status})`);
  const body = await resp.json();
  return { username: body.username, role: body.role };
}

/**
 * Two modes in one component:
 *  - "login": username/password against POST /auth/login.
 *  - "change": forced password change (either because the login response
 *    carried must_change_password, or because App already holds a valid
 *    token for a user who still needs to change — see App.tsx's mount
 *    check) against POST /auth/change-password.
 * Either path ends by fetching /auth/me and handing the result up via
 * onDone, which is how App learns the signed-in identity.
 */
export default function Login({
  onDone,
  initialMode = "login",
  initialUsername = "",
}: {
  onDone: (me: Me) => void;
  initialMode?: "login" | "change";
  initialUsername?: string;
}) {
  const [mode, setMode] = useState<"login" | "change">(initialMode);
  const [username, setUsername] = useState(initialUsername);
  const [password, setPassword] = useState("");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Presentation-only: mirrors the changePassword handler's mismatch check
  // so the Confirm field gets the mockup's red error state as soon as the
  // user has typed a differing confirmation, without touching when/how the
  // actual submit-time validation (and its error copy) runs.
  const confirmMismatch =
    confirmPassword.length > 0 && newPassword !== confirmPassword;

  async function login(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const resp = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setError(typeof body.detail === "string" ? body.detail : `HTTP ${resp.status}`);
        return;
      }
      setToken(body.token);
      if (body.must_change_password) {
        setPassword("");
        setMode("change");
        return;
      }
      onDone(await fetchMe());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("new password and confirmation do not match");
      return;
    }
    setBusy(true);
    try {
      // skip401Handling: a wrong old_password is a business-logic 401 on a
      // still-valid session, not session expiry — must not clear the token
      // (that would strand the user with no Authorization header to retry).
      const resp = await apiFetch("/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      }, { skip401Handling: true });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setError(typeof body.detail === "string" ? body.detail : `HTTP ${resp.status}`);
        return;
      }
      onDone(await fetchMe());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="tk-card login-card">
        <div className="login-header">
          <div className="tk-logo-mark">T</div>
          <div className="login-title">Timeplus Knowledge</div>
        </div>
        {mode === "login" ? (
          <form className="login-form" onSubmit={login}>
            <label className="login-field">
              <span className="login-label">Username</span>
              <input className="tk-input" value={username} autoFocus required
                     onChange={(e) => setUsername(e.target.value)} />
            </label>
            <label className="login-field">
              <span className="login-label">Password</span>
              <input className="tk-input" type="password" value={password} required
                     onChange={(e) => setPassword(e.target.value)} />
            </label>
            {error && <div className="error">{error}</div>}
            <button type="submit" className="tk-btn login-submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        ) : (
          <form className="login-form" onSubmit={changePassword}>
            <p className="login-subtext">You must change your password before continuing.</p>
            <label className="login-field">
              <span className="login-label">Current password</span>
              <input className="tk-input" type="password" value={oldPassword} required autoFocus
                     onChange={(e) => setOldPassword(e.target.value)} />
            </label>
            <label className="login-field">
              <span className="login-label">New password</span>
              <input className="tk-input" type="password" value={newPassword} required
                     onChange={(e) => setNewPassword(e.target.value)} />
            </label>
            <label className={`login-field${confirmMismatch ? " login-field-error" : ""}`}>
              <span className="login-label">Confirm new password</span>
              <input className="tk-input" type="password" value={confirmPassword} required
                     onChange={(e) => setConfirmPassword(e.target.value)} />
            </label>
            {error && <div className="error">{error}</div>}
            <button type="submit" className="tk-btn login-submit" disabled={busy}>
              {busy ? "Saving…" : "Change password"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
