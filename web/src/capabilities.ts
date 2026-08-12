// Function capabilities a role can grant (mirrors tpk.auth). `:manage`
// implies `:view` — the backend expands it, and the role editor mirrors that
// by auto-selecting the view sibling when manage is checked.

export const CAP = {
  chat: "chat",
  explore: "explore",
  corpusView: "corpus:view",
  corpusManage: "corpus:manage",
  usersView: "users:view",
  usersManage: "users:manage",
} as const;

export type Capability = (typeof CAP)[keyof typeof CAP];

// Canonical display order + labels for the role editor.
export const CAPABILITY_OPTIONS: { key: Capability; label: string; implies?: Capability }[] = [
  { key: CAP.chat, label: "Chat" },
  { key: CAP.explore, label: "Explore" },
  { key: CAP.corpusView, label: "Corpus — view" },
  { key: CAP.corpusManage, label: "Corpus — manage", implies: CAP.corpusView },
  { key: CAP.usersView, label: "Users — view" },
  { key: CAP.usersManage, label: "Users — manage", implies: CAP.usersView },
];

const MANAGE_IMPLIES_VIEW: Record<string, Capability> = {
  [CAP.corpusManage]: CAP.corpusView,
  [CAP.usersManage]: CAP.usersView,
};

export function hasCap(caps: string[] | undefined, cap: Capability): boolean {
  return !!caps && caps.includes(cap);
}

// Expand a selected set so every `:manage` carries its `:view` sibling —
// keeps the stored/sent set consistent with backend enforcement.
export function expandCaps(caps: string[]): string[] {
  const out = new Set(caps);
  for (const c of caps) {
    const implied = MANAGE_IMPLIES_VIEW[c];
    if (implied) out.add(implied);
  }
  return [...out];
}
