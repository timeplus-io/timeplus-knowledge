import { useEffect, useState } from "react";
import { apiFetch } from "./api";

// A lightweight new-password strength score (mockup 5b): length + character
// variety, mapped to a fill width and a label. Presentation only — the
// backend is the authority on what's accepted.
function passwordStrength(pw: string): { pct: number; label: string } {
  if (!pw) return { pct: 0, label: "" };
  let score = 0;
  if (pw.length >= 8) score++;
  if (pw.length >= 12) score++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
  if (/\d/.test(pw)) score++;
  if (/[^A-Za-z0-9]/.test(pw)) score++;
  if (pw.length < 8) return { pct: 20, label: "too short" };
  const steps = [
    { pct: 40, label: "weak" },
    { pct: 60, label: "fair" },
    { pct: 78, label: "good" },
    { pct: 90, label: "good" },
    { pct: 100, label: "strong" },
  ];
  return steps[Math.min(score, steps.length) - 1];
}

// Self-service password change for any logged-in user (issue #27). Reuses the
// same POST /auth/change-password endpoint and validation as Login's forced
// change flow; the identity doesn't change and the current session stays
// valid (the endpoint revokes only the user's OTHER sessions), so on success
// we just confirm and close — no re-login or /auth/me refetch needed.
export default function ChangePassword({ username, onClose }:
    { username: string; onClose: () => void }) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Live mismatch feedback, mirroring Login's change form.
  const confirmMismatch =
    confirmPassword.length > 0 && newPassword !== confirmPassword;

  const strength = passwordStrength(newPassword);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("new password and confirmation do not match");
      return;
    }
    setBusy(true);
    try {
      // skip401Handling: a wrong current password is a business-logic 401 on a
      // still-valid session, not session expiry — must not clear the token.
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
      setDone(true);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tk-modal-overlay" onClick={onClose}>
      <div
        className="tk-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cp-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="tk-modal-header">
          <div id="cp-title" className="tk-modal-title">Change password</div>
          <div className="tk-modal-spacer" />
          <button type="button" className="tk-modal-close" aria-label="Close" onClick={onClose}>
            &times;
          </button>
        </div>
        {done ? (
          <>
            <div className="tk-modal-body">
              <div className="tk-form-hint">
                Your password has been changed. Any other active sessions were signed out.
              </div>
            </div>
            <div className="tk-modal-footer">
              <button type="button" className="tk-btn" onClick={onClose} autoFocus>Done</button>
            </div>
          </>
        ) : (
          <form onSubmit={submit}>
            <div className="tk-modal-body">
              <div className="tk-form-hint">
                Signed in as <strong>{username}</strong> · you'll stay signed in on this device.
              </div>
              <div className="tk-form-field">
                <label htmlFor="cp-old">
                  Current password <span className="tk-form-required">*</span>
                </label>
                <input id="cp-old" className="tk-input" type="password" required autoFocus
                       value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} />
              </div>
              <div className="tk-form-field">
                <label htmlFor="cp-new">
                  New password <span className="tk-form-required">*</span>
                </label>
                <input id="cp-new" className="tk-input" type="password" required
                       value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
                {newPassword && (
                  <div className="tk-strength">
                    <div className="tk-strength-track">
                      <div className="tk-strength-fill" style={{ width: `${strength.pct}%` }} />
                    </div>
                    <div className="tk-strength-label">{strength.label}</div>
                  </div>
                )}
                <div className="tk-form-hint">
                  At least 8 characters; must differ from the current password.
                </div>
              </div>
              <div className="tk-form-field">
                <label htmlFor="cp-confirm">
                  Confirm new password <span className="tk-form-required">*</span>
                </label>
                <input id="cp-confirm" className="tk-input" type="password" required
                       value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} />
              </div>
              {confirmMismatch && (
                <div className="tk-manage-error">new password and confirmation do not match</div>
              )}
              {error && !(confirmMismatch && error === "new password and confirmation do not match") && (
                <div className="tk-manage-error">{error}</div>
              )}
            </div>
            <div className="tk-modal-footer">
              <button type="button" className="tk-btn tk-btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="tk-btn" disabled={busy || confirmMismatch}>
                {busy ? "Saving…" : "Change password"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
