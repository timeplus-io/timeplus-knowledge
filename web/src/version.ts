// The SERVER's version, from /healthz -- not the bundle's build-time one, so
// the UI always reports what is actually running (#85).
import { useEffect, useState } from "react";

export type ServerVersion = { version: string; commit: string };

let cached: Promise<ServerVersion | null> | null = null;

function load(): Promise<ServerVersion | null> {
  cached ??= fetch("/healthz")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => (d && typeof d.version === "string"
      ? { version: d.version, commit: typeof d.commit === "string" ? d.commit : "" }
      : null))
    .catch(() => null);
  return cached;
}

export function useServerVersion(): ServerVersion | null {
  const [v, setV] = useState<ServerVersion | null>(null);
  useEffect(() => {
    let alive = true;
    load().then((x) => { if (alive) setV(x); });
    return () => { alive = false; };
  }, []);
  return v;
}

// "v0.0.5" for a release, the raw string otherwise ("dev", "dev-bc549ef", ...).
export function versionLabel(v: ServerVersion): string {
  return /^\d/.test(v.version) ? `v${v.version}` : v.version;
}
