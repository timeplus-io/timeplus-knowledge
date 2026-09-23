import { useEffect, useRef, useState, type ReactNode } from "react";
import { useServerVersion, versionLabel } from "./version";
import { CAP, type Capability, hasCap } from "./capabilities";

// The app-wide sidebar shell. Two widths (mockup t6): expanded (220px, icon +
// label) and a collapsed 56px icon rail with hover tooltips. Every
// authenticated view renders inside the `.tk-content` slot; the Login gate
// stays outside (App.tsx renders it on the centered `.shell`).
export type View = "chat" | "explorer" | "manage" | "users" | "tokens";

type Me = { username: string; role: string; capabilities: string[]; anonymous?: boolean };

const NAV_ITEMS: { key: View; label: string; cap: Capability }[] = [
  { key: "chat", label: "Chat", cap: CAP.chat },
  { key: "explorer", label: "Explorer", cap: CAP.explore },
  { key: "tokens", label: "API tokens", cap: CAP.explore },
  { key: "manage", label: "Manage", cap: CAP.corpusView },
  { key: "users", label: "Users", cap: CAP.usersView },
];

// Line icons (18px, stroke=currentColor) matching the mockup.
const NAV_ICON: Record<View, ReactNode> = {
  chat: (
    <path d="M21 12c0 4.1-4 7.4-9 7.4-1.1 0-2.2-.16-3.2-.46L4 20.5l1.2-3.4C4.1 15.8 3 14 3 12c0-4.1 4-7.4 9-7.4s9 3.3 9 7.4z" />
  ),
  explorer: (
    <>
      <circle cx="6" cy="6" r="2.6" />
      <circle cx="18" cy="8" r="2.6" />
      <circle cx="12" cy="18" r="2.6" />
      <path d="M8.4 7 15.5 8m-8 3.1 3.4 4.6m5.7-5.3-3 4.9" />
    </>
  ),
  tokens: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="M10.8 12.2 20 3m-3.5 3.5 2.5 2.5M14 9l2 2" />
    </>
  ),
  manage: (
    <>
      <ellipse cx="12" cy="5.5" rx="8" ry="2.8" />
      <path d="M4 5.5V12c0 1.55 3.58 2.8 8 2.8s8-1.25 8-2.8V5.5" />
      <path d="M4 12v6.5c0 1.55 3.58 2.8 8 2.8s8-1.25 8-2.8V12" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5" />
      <path d="M16 5.2a3.2 3.2 0 0 1 0 5.9m2 8.4c0-2.4-1.3-4.1-3.2-4.8" />
    </>
  ),
};

function NavSvg({ children }: { children: ReactNode }) {
  return (
    <svg className="tk-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

const COLLAPSE_KEY = "tk-sidebar-collapsed";

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
  const serverVersion = useServerVersion();
  // Collapsed state persists across navigation and reloads.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(COLLAPSE_KEY) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0"); } catch { /* ignore */ }
  }, [collapsed]);

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
      <div className={collapsed ? "tk-sidebar collapsed" : "tk-sidebar"}>
        <div className="tk-logo">
          <div className="tk-logo-mark">T</div>
          <div className="tk-logo-text">
            <div className="tk-logo-title">Timeplus</div>
            <div className="tk-logo-subtitle">Knowledge</div>
          </div>
        </div>
        {NAV_ITEMS.filter((item) => hasCap(me.capabilities, item.cap)).map((item) => (
          <button
            key={item.key}
            type="button"
            className={item.key === view ? "tk-nav-item active" : "tk-nav-item"}
            aria-current={item.key === view ? "page" : undefined}
            onClick={() => onNavigate(item.key)}
          >
            <NavSvg>{NAV_ICON[item.key]}</NavSvg>
            <span className="tk-nav-label">{item.label}</span>
          </button>
        ))}
        <div className="tk-nav-spacer" />
        <button
          type="button"
          className="tk-nav-collapse"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={() => setCollapsed((v) => !v)}
        >
          <svg className="tk-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d={collapsed ? "M9 6l6 6-6 6" : "M15 6l-6 6 6 6"} />
          </svg>
          <span className="tk-nav-collapse-label">Collapse</span>
        </button>
        {serverVersion && (
          <div className="tk-version" title={serverVersion.commit
            ? `tpk ${serverVersion.version} (${serverVersion.commit})` : `tpk ${serverVersion.version}`}>
            {versionLabel(serverVersion)}
          </div>
        )}
        {me.anonymous ? (
          <div className="tk-account tk-account-anonymous">
            <div className="tk-account-anon-badge" title="Unauthenticated: only the public corpus is searchable">
              Browsing the public docs
            </div>
            <button type="button" className="tk-btn tk-btn-secondary tk-account-signin" onClick={onLogout}>
              Sign in
            </button>
          </div>
        ) : (
        <div className="tk-account" ref={accountRef}>
          <button
            type="button"
            className={menuOpen ? "tk-account-trigger open" : "tk-account-trigger"}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label={`Account menu for ${me.username}`}
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
        )}
      </div>
      <div className="tk-content">{children}</div>
    </div>
  );
}
