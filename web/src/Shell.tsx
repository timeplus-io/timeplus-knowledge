import type { ReactNode } from "react";
import { CAP, type Capability, hasCap } from "./capabilities";

// The app-wide sidebar shell (mockup screens 1a/1d/1e/1f share this exact
// layout: 220px sidebar with a T-logo, nav items, a flex spacer, and a
// bottom account row). Every authenticated view renders inside it via the
// `.tk-content` slot; the Login gate stays outside (App.tsx renders it on
// the old centered `.shell`, never wrapped in Shell).
export type View = "chat" | "explorer" | "manage" | "users";

type Me = { username: string; role: string; capabilities: string[] };

// Each nav entry is shown only when the caller holds its capability. Manage
// and Users open on their read view (`:view`, implied by `:manage`).
const NAV_ITEMS: { key: View; label: string; cap: Capability }[] = [
  { key: "chat", label: "Chat", cap: CAP.chat },
  { key: "explorer", label: "Explorer", cap: CAP.explore },
  { key: "manage", label: "Manage", cap: CAP.corpusView },
  { key: "users", label: "Users", cap: CAP.usersView },
];

export default function Shell({
  me,
  view,
  onNavigate,
  onLogout,
  children,
}: {
  me: Me;
  view: View;
  onNavigate: (view: View) => void;
  onLogout: () => void;
  children: ReactNode;
}) {
  return (
    <div className="tk-shell">
      <div className="tk-sidebar">
        <div className="tk-logo">
          <div className="tk-logo-mark">T</div>
          <div>
            <div className="tk-logo-title">Timeplus</div>
            <div className="tk-logo-subtitle">Knowledge</div>
          </div>
        </div>
        {NAV_ITEMS.filter((item) => hasCap(me.capabilities, item.cap)).map((item) => (
          <button
            key={item.key}
            type="button"
            className={item.key === view ? "tk-nav-item active" : "tk-nav-item"}
            onClick={() => onNavigate(item.key)}
          >
            {item.label}
          </button>
        ))}
        <div className="tk-nav-spacer" />
        <div className="tk-account">
          <div className="tk-avatar">{me.username.slice(0, 1).toUpperCase()}</div>
          <div className="tk-account-name">{me.username}</div>
          <button type="button" className="tk-logout" onClick={onLogout}>Logout</button>
        </div>
      </div>
      <div className="tk-content">{children}</div>
    </div>
  );
}
