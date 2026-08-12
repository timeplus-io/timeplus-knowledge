import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { apiFetch, getToken, onPasswordChangeRequired, onUnauthorized, setToken } from "./api";
import Login from "./Login";
import Manage from "./Manage";
import Users from "./Users";

// Models emit <br> inside GFM table cells (cells cannot hold real
// newlines). rehype-raw parses raw HTML, then rehype-sanitize strips it
// back to a GitHub-grade safe subset: scripts, styles, iframes and all
// event-handler attributes are removed. We additionally drop img so model
// output can never load external resources (tracking pixels).
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== "img"),
};

type Turn = { role: "user" | "assistant"; content: string; tools?: string[] };
type Me = { username: string; role: string };

async function* sseEvents(resp: Response): AsyncGenerator<any> {
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (frame.startsWith("data: ")) yield JSON.parse(frame.slice(6));
    }
  }
}

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"chat" | "manage" | "users">("chat");
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auth: null `me` (and no pending change) renders the Login gate. `checked`
  // guards the one render before the mount-time /auth/me check resolves, so
  // a logged-in reload doesn't flash the login form.
  const [me, setMe] = useState<Me | null>(null);
  const [pendingChangeUser, setPendingChangeUser] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    onUnauthorized(() => { setMe(null); setPendingChangeUser(null); });
    // Centralized in api.ts: fires on a 403 password_change_required from
    // ANY apiFetch call (chat, Manage, Users) — not just chat's send().
    // Uses the setMe functional-updater form to read the current identity
    // without a stale closure over `me`.
    onPasswordChangeRequired(() => {
      setMe((prev) => { setPendingChangeUser(prev?.username ?? null); return null; });
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!getToken()) { setChecked(true); return; }
      try {
        const resp = await apiFetch("/auth/me");
        if (cancelled) return;
        if (resp.ok) {
          const body = await resp.json();
          // A must_change_password answer routes straight to Login's change
          // mode (skipping the login form — we already hold a valid token).
          if (body.must_change_password) setPendingChangeUser(body.username);
          else setMe({ username: body.username, role: body.role });
        }
        // A 401 here already ran onUnauthorized above (token cleared, me null).
      } catch {
        // Network-level failure (e.g. server restarting on page load): fall
        // through to the login gate instead of leaving the app stuck on a
        // blank screen forever.
        if (cancelled) return;
        setToken(null);
        setMe(null);
      } finally {
        if (!cancelled) setChecked(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function logout() {
    try {
      await apiFetch("/auth/logout", { method: "POST" });
    } catch {
      // Even if the network request fails, always clear the local session
      // so the user isn't stranded in the authenticated view.
    } finally {
      setToken(null);
      setMe(null);
      setView("chat");
    }
  }

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((ts) => [...ts, { role: "user", content: message }, { role: "assistant", content: "", tools: [] }]);

    const update = (fn: (t: Turn) => Turn) =>
      setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

    try {
      // A 403 password_change_required here is handled centrally by
      // api.ts's onPasswordChangeRequired hook (registered above), which
      // routes to the change screen before this call sees the response.
      const resp = await apiFetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      if (!resp.ok || !resp.body) {
        update((t) => ({ ...t, content: t.content + `\n\n[error] HTTP ${resp.status}` }));
        return;
      }
      for await (const ev of sseEvents(resp)) {
        if (ev.type === "token") update((t) => ({ ...t, content: t.content + ev.text }));
        else if (ev.type === "tool") update((t) => ({ ...t, tools: [...(t.tools ?? []), ev.name] }));
        else if (ev.type === "done") update((t) => ({ ...t, content: ev.text }));
        else if (ev.type === "error") update((t) => ({ ...t, content: t.content + `\n\n[error] ${ev.message}` }));
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    } catch (e) {
      update((t) => ({ ...t, content: t.content + `\n\n[error] ${String(e)}` }));
    } finally {
      setBusy(false);
    }
  }

  if (!checked) return null;

  if (pendingChangeUser !== null) {
    return (
      <div className="shell">
        <Login initialMode="change" initialUsername={pendingChangeUser}
               onDone={(m) => { setPendingChangeUser(null); setMe(m); }} />
      </div>
    );
  }

  if (!me) {
    return (
      <div className="shell">
        <Login onDone={setMe} />
      </div>
    );
  }

  return (
    <div className="shell">
      <header>
        <div className="header-row">
          <div>
            <h1>Timeplus Knowledge</h1>
            <p>Ask anything about Timeplus — code, architecture, deployment.</p>
          </div>
          <nav className="tabs">
            <button className={view === "chat" ? "tab active" : "tab"}
                    onClick={() => setView("chat")}>Chat</button>
            {me.role === "admin" && (
              <button className={view === "manage" ? "tab active" : "tab"}
                      onClick={() => setView("manage")}>Manage</button>
            )}
            {me.role === "admin" && (
              <button className={view === "users" ? "tab active" : "tab"}
                      onClick={() => setView("users")}>Users</button>
            )}
          </nav>
          <div className="account">
            <span className="muted">{me.username}</span>
            <button className="secondary" onClick={logout}>Logout</button>
          </div>
        </div>
      </header>
      {view === "chat" ? (
        <>
          <main>
            {turns.map((t, i) => (
              <div key={i} className={`turn ${t.role}`}>
                {t.tools && t.tools.length > 0 && (
                  <div className="tools">🔎 {t.tools.join(" → ")}</div>
                )}
                <div className="bubble">
                  {t.role === "assistant" && t.content ? (
                    // Raw HTML from the model is sanitized to a safe subset
                    // (see sanitizeSchema above) — scripts/handlers/img never
                    // reach the DOM, but <br> in table cells renders.
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
                    >
                      {t.content}
                    </ReactMarkdown>
                  ) : (
                    t.content || (busy && i === turns.length - 1 ? "…" : "")
                  )}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </main>
          <footer>
            <textarea
              value={input}
              placeholder="e.g. How do materialized view checkpoints work?"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
            />
            <button onClick={send} disabled={busy || !input.trim()}>
              {busy ? "Thinking…" : "Send"}
            </button>
          </footer>
        </>
      ) : view === "manage" ? <Manage /> : <Users />}
    </div>
  );
}
