import { useEffect, useRef, useState, type ReactNode } from "react";
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
  onChangePassword,
  children,
}: {
  me: Me;
  view: View;
  onNavigate: (view: View) => void;
  onLogout: () => void;
  onChangePassword: () => void;
  children: ReactNode;
}) {
  // Account menu popover (mockup 5a): click the footer row to open a menu
  // with Change password / Logout. Closes on outside-click or Escape.
  const [menuOpen, setMenuOpen] = useState(false);
  const accountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (accountRef.current && !accountRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenuOpen(false); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

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
        <div className="tk-account" ref={accountRef}>
          {/* Trigger before the menu in DOM so keyboard Tab reaches the menu
              items after opening; CSS bottom:100% still floats the menu above. */}
          <button
            type="button"
            className={menuOpen ? "tk-account-trigger open" : "tk-account-trigger"}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          >
            <div className="tk-avatar">{me.username.slice(0, 1).toUpperCase()}</div>
            <div className="tk-account-name">{me.username}</div>
            <div className="tk-account-caret" aria-hidden="true">{menuOpen ? "▾" : "▴"}</div>
          </button>
          {menuOpen && (
            <div className="tk-account-menu" role="menu">
              <div className="tk-account-menu-header">
                <div className="tk-account-menu-name">{me.username}</div>
                <div className="tk-account-menu-role">{me.role}</div>
              </div>
              <button
                type="button"
                role="menuitem"
                className="tk-account-menu-item"
                onClick={() => { setMenuOpen(false); onChangePassword(); }}
              >
                Change password…
              </button>
              <button
                type="button"
                role="menuitem"
                className="tk-account-menu-item tk-account-menu-item-danger"
                onClick={() => { setMenuOpen(false); onLogout(); }}
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </div>
      <div className="tk-content">{children}</div>
    </div>
  );
}
