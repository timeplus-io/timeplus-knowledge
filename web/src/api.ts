// Shared auth helpers: bearer token storage + a fetch wrapper that attaches
// it and reacts to 401s. Session-scoped (sessionStorage), not persisted
// across browser restarts — matches the backend's session-token model.

let unauthorized: (() => void) | null = null;

export function onUnauthorized(cb: () => void) {
  unauthorized = cb;
}

export function getToken(): string | null {
  return sessionStorage.getItem("tpk_token");
}

export function setToken(t: string | null) {
  if (t) sessionStorage.setItem("tpk_token", t);
  else sessionStorage.removeItem("tpk_token");
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401) {
    setToken(null);
    unauthorized?.();
  }
  return resp;
}
