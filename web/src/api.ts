// Shared auth helpers: bearer token storage + a fetch wrapper that attaches
// it and reacts to session-level failures. Session-scoped (sessionStorage),
// not persisted across browser restarts — matches the backend's
// session-token model.

let unauthorized: (() => void) | null = null;
let passwordChangeRequired: (() => void) | null = null;

export function onUnauthorized(cb: () => void) {
  unauthorized = cb;
}

// Fires when any apiFetch call comes back 403 {"detail": {"code":
// "password_change_required"}} — the gate the backend puts on /chat and
// /api/* while a user's must_change_password flag is set (e.g. flipped
// mid-session by another admin's password reset). Centralized here so
// every apiFetch caller (chat, Manage, Users) gets the same redirect
// without each one special-casing the response body.
export function onPasswordChangeRequired(cb: () => void) {
  passwordChangeRequired = cb;
}

export function getToken(): string | null {
  return sessionStorage.getItem("tpk_token");
}

export function setToken(t: string | null) {
  if (t) sessionStorage.setItem("tpk_token", t);
  else sessionStorage.removeItem("tpk_token");
}

export async function apiFetch(
  path: string,
  init: RequestInit = {},
  opts: { skip401Handling?: boolean } = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...init, headers });
  // skip401Handling: for endpoints where a 401 is a business-logic
  // response on an otherwise-still-valid session (e.g. change-password
  // rejecting a wrong old password) rather than session expiry — clearing
  // the token there would strand the user with no way to retry.
  // A 401 means "your session is gone" only when there WAS a session token
  // to lose. An anonymous session (#94) holds no token and gets 401 from
  // every non-chat endpoint by design; that must not bounce it to login.
  if (resp.status === 401 && !opts.skip401Handling && token) {
    setToken(null);
    unauthorized?.();
  }
  if (resp.status === 403 && passwordChangeRequired) {
    // Peek at the body via clone() so the caller can still read the
    // original response (Response bodies can only be consumed once).
    try {
      const body = await resp.clone().json();
      if (body?.detail?.code === "password_change_required") passwordChangeRequired();
    } catch {
      // non-JSON 403 body — not our signal, ignore
    }
  }
  return resp;
}
