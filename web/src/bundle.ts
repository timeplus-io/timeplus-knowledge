// Read `manifest.json` out of a corpus bundle archive (.tar.gz) in the
// browser, so the Import modal can offer the bundle's entries as a pick list
// (#104) without a second upload or a server-side staged upload.
//
// `pack_bundle` writes the manifest as the FIRST tar member, so only the
// head of the archive needs inflating: the gzip stream is read through
// DecompressionStream just far enough to reach the manifest's bytes, then
// cancelled. A tar member is a 512-byte header (name at 0..100, size in
// octal at 124..136, type flag at 156) followed by the content padded to a
// 512-byte boundary; PAX extended headers (type x/g) are skipped.

export type BundleManifest = {
  bundle_version?: number;
  format?: string;
  created_at?: string;
  partial?: boolean;
  entries?: string[];
  streams?: Record<string, { file: string; columns: string[]; rows: number }>;
};

const BLOCK = 512;
const MANIFEST = "manifest.json";
const MAX_PEEK = 4 * 1024 * 1024; // give up if the manifest isn't within 4 MB
const MAX_MEMBERS = 8;

function field(bytes: Uint8Array, off: number, len: number): string {
  let end = off;
  while (end < off + len && bytes[end] !== 0) end++;
  return new TextDecoder().decode(bytes.subarray(off, end));
}

function octal(bytes: Uint8Array, off: number, len: number): number {
  const s = field(bytes, off, len).trim();
  return s ? parseInt(s, 8) : 0;
}

export async function peekManifest(file: File): Promise<BundleManifest | null> {
  if (typeof DecompressionStream === "undefined" || typeof file.stream !== "function") return null;
  const reader = file.stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  let buf = new Uint8Array(0);
  async function ensure(n: number): Promise<boolean> {
    while (buf.length < n) {
      if (n > MAX_PEEK) return false;
      const { value, done } = await reader.read();
      if (done) return false;
      const next = new Uint8Array(buf.length + value.length);
      next.set(buf);
      next.set(value, buf.length);
      buf = next;
    }
    return true;
  }
  try {
    let off = 0;
    for (let i = 0; i < MAX_MEMBERS; i++) {
      if (!(await ensure(off + BLOCK))) return null;
      const hdr = buf.subarray(off, off + BLOCK);
      if (hdr.every((b) => b === 0)) return null; // end-of-archive marker
      const name = field(hdr, 0, 100);
      const size = octal(hdr, 124, 12);
      const type = String.fromCharCode(hdr[156]);
      const dataStart = off + BLOCK;
      if (name === MANIFEST && (type === "0" || type === "\0")) {
        if (!(await ensure(dataStart + size))) return null;
        const parsed = JSON.parse(new TextDecoder().decode(buf.subarray(dataStart, dataStart + size)));
        return parsed && typeof parsed === "object" && parsed.streams ? (parsed as BundleManifest) : null;
      }
      off = dataStart + Math.ceil(size / BLOCK) * BLOCK;
    }
    return null;
  } catch {
    return null; // not gzip, not tar, not JSON: the server will say so on upload
  } finally {
    reader.cancel().catch(() => undefined);
  }
}
