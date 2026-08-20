import { useEffect, useState } from "react";
import { apiFetch, getToken, onPasswordChangeRequired, onUnauthorized, setToken } from "./api";
import Chat from "./Chat";
import Explorer from "./Explorer";
import Login from "./Login";
import Manage from "./Manage";
import Shell, { type View } from "./Shell";
import Users from "./Users";
import ChangePassword from "./ChangePassword";
import { CAP, type Capability, hasCap } from "./capabilities";

type Me = { username: string; role: string; capabilities: string[] };

// Nav order + the capability each view needs, so a caller lands on the first
// view they're allowed to see (a chat-less role must not open on Chat).
const VIEW_CAP: Record<View, Capability> = {
  chat: CAP.chat,
  explorer: CAP.explore,
  manage: CAP.corpusView,
  users: CAP.usersView,
};
const NAV_ORDER: View[] = ["chat", "explorer", "manage", "users"];

function landingView(caps: string[]): View {
  return NAV_ORDER.find((v) => hasCap(caps, VIEW_CAP[v])) ?? "chat";
}

export default function App() {
  const [view, setView] = useState<View>("chat");

  // Auth: null `me` (and no pending change) renders the Login gate. `checked`
  // guards the one render before the mount-time /auth/me check resolves, so
  // a logged-in reload doesn't flash the login form.
  const [me, setMe] = useState<Me | null>(null);
  const [pendingChangeUser, setPendingChangeUser] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  // Explorer's "Ask about this" -> Chat handoff: a question typed here is
  // stashed until the view switches to Chat, which consumes it into its
  // input box (via initialInput/onConsumeInitial) and clears it so it
  // doesn't reappear on a later visit to Chat.
  const [chatPrefill, setChatPrefill] = useState<string | undefined>(undefined);

  // Self-service password change (issue #27), opened from the sidebar account
  // row; independent of the forced-change flow above.
  const [changePwOpen, setChangePwOpen] = useState(false);

  function askAboutEntity(text: string) {
    setChatPrefill(text);
    setView("chat");
  }

  useEffect(() => {
    onUnauthorized(() => { setMe(null); setPendingChangeUser(null); setChangePwOpen(false); });
    // Centralized in api.ts: fires on a 403 password_change_required from
    // ANY apiFetch call (chat, Manage, Users) — not just chat's send().
    // Uses the setMe functional-updater form to read the current identity
    // without a stale closure over `me`.
    onPasswordChangeRequired(() => {
      setChangePwOpen(false);
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
          else setMe({ username: body.username, role: body.role,
                       capabilities: body.capabilities ?? [] });
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

  // When identity resolves (login or reload), fall back to the first
  // permitted view if the current one isn't allowed for this role.
  useEffect(() => {
    if (!me) return;
    if (!hasCap(me.capabilities, VIEW_CAP[view])) setView(landingView(me.capabilities));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me]);

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
      setChangePwOpen(false);
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
    <Shell me={me} view={view} onNavigate={setView} onLogout={logout}
           onChangePassword={() => setChangePwOpen(true)}>
      {/* Chat stays mounted across nav (unlike Explorer/Manage/Users, which
          are cheap to remount) so its turns state and any in-flight SSE
          stream survive a Chat -> Explorer -> Chat round trip; it's just
          hidden via display:none while another view is active. See the
          final-review regression this fixes: App.tsx used to unmount Chat
          on every navigation. */}
      <div className={view === "chat" ? "tk-chat-slot" : "tk-chat-slot tk-hidden"}>
        <Chat initialInput={chatPrefill} onConsumeInitial={() => setChatPrefill(undefined)}
              canViewSource={hasCap(me.capabilities, CAP.sourceView)} />
      </div>
      {view === "explorer" ? (
        <Explorer onAsk={askAboutEntity} />
      ) : view === "manage" ? (
        <Manage capabilities={me.capabilities} />
      ) : view === "users" ? (
        <Users capabilities={me.capabilities} isAdmin={me.role === "admin"} />
      ) : null}
      {changePwOpen && (
        <ChangePassword username={me.username} onClose={() => setChangePwOpen(false)} />
      )}
    </Shell>
  );
}
