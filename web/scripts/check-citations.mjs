// Permanent XSS-safety + correctness gate for Chat.tsx's citation
// superscript feature ([n] -> <sup><a href="#tk-source-n">[n]</a></sup>).
//
// citationPlugin runs AFTER rehypeRaw + rehypeSanitize in Chat.tsx's real
// rehypePlugins array. If a future change makes it interpolate untrusted
// text into raw HTML (instead of only ever constructing sup/a hast nodes
// itself), that would reintroduce XSS *downstream* of sanitize, and
// check:sanitize alone would never see it (it doesn't touch citationPlugin
// at all). This script closes that gap by rendering the EXACT same
// rehypePlugins array Chat.tsx uses.
//
// citationPlugin and sanitizeSchema are imported from the real Chat.tsx
// (via an esbuild-compiled temp module), not copy-pasted, so this test
// covers the actual shipped code and can't silently drift from it.
//
// Run: npm run check:citations
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import * as esbuild from "esbuild";
import { mkdtempSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(__dirname, "..");
const chatEntry = path.join(webDir, "src", "Chat.tsx");

// Compile the real Chat.tsx (TSX, stripped of types) to a self-contained
// ESM module so we can import its actual `citationPlugin`/`sanitizeSchema`
// exports from a plain Node script. Everything Chat.tsx imports is bundled
// in EXCEPT the npm packages we also import here directly (kept external
// so both this script and the compiled module share one instance of each,
// and so the compiled file's own `import "react"` etc. resolve). It must
// be a plain subdirectory of web/ -- NOT inside web/node_modules itself,
// since Node's module resolution algorithm stops walking upward the
// moment it reaches a directory literally named "node_modules" and so
// would never find web/node_modules from underneath it.
const tmpDir = mkdtempSync(path.join(webDir, ".tk-check-citations-"));
const outfile = path.join(tmpDir, "chat-under-test.mjs");

let citationPlugin, sanitizeSchema;
try {
  await esbuild.build({
    entryPoints: [chatEntry],
    outfile,
    bundle: true,
    format: "esm",
    platform: "node",
    jsx: "automatic",
    external: ["react", "react/jsx-runtime", "react-dom", "react-markdown", "rehype-raw", "rehype-sanitize", "remark-gfm"],
    logLevel: "silent",
  });
  ({ citationPlugin, sanitizeSchema } = await import(pathToFileURL(outfile).href));
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (typeof citationPlugin !== "function") throw new Error("Chat.tsx did not export citationPlugin");
if (!sanitizeSchema || !Array.isArray(sanitizeSchema.tagNames)) {
  throw new Error("Chat.tsx did not export a usable sanitizeSchema");
}

// The exact rehypePlugins array Chat.tsx passes to ReactMarkdown for an
// assistant bubble (see Chat.tsx's render()).
function render(md, sourceCount) {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, {
      remarkPlugins: [remarkGfm],
      rehypePlugins: [rehypeRaw, [rehypeSanitize, sanitizeSchema], [citationPlugin, sourceCount]],
    }, md),
  );
}

const checks = [];
function check(name, cond) {
  checks.push([name, cond]);
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
}

// --- correctness ---------------------------------------------------------

const inRange = render("Checkpoints persist state.[1] Interval is tunable.[2]", 2);
check(
  "in-range [1] -> sup>a href=#tk-source-1 class=tk-citation",
  /<sup><a href="#tk-source-1" class="tk-citation">\[1\]<\/a><\/sup>/.test(inRange),
);
check(
  "in-range [2] -> sup>a href=#tk-source-2 class=tk-citation",
  /<sup><a href="#tk-source-2" class="tk-citation">\[2\]<\/a><\/sup>/.test(inRange),
);

const outOfRange = render("See [5] for details.", 2);
check(
  "out-of-range [5] (sourceCount=2) left as plain text",
  outOfRange.includes("[5]") && !outOfRange.includes('href="#tk-source-5"') && !outOfRange.includes("tk-citation"),
);

const noSources = render("array[1] access example.", 0);
check("sourceCount=0 leaves everything untouched", !noSources.includes("tk-citation") && noSources.includes("array[1]"));

const inlineCode = render("Use `array[1]` to index.", 3);
check(
  "[n]-shaped text inside inline code is not rewritten",
  inlineCode.includes("<code>array[1]</code>") && !inlineCode.includes("tk-citation"),
);

const fencedCode = render("```\nconst x = arr[1];\n```", 3);
check(
  "[n]-shaped text inside a fenced code block is not rewritten",
  fencedCode.includes("arr[1]") && !fencedCode.includes("tk-citation"),
);

const multiDigit = render("See the full trace.[12]", 12);
check(
  "multi-digit [12] parses as n=12, not two separate digits",
  /<sup><a href="#tk-source-12" class="tk-citation">\[12\]<\/a><\/sup>/.test(multiDigit),
);

// --- adversarial / XSS safety --------------------------------------------
// The property that matters here is that a `javascript:` scheme (or any
// executable markup) never survives to the rendered output, regardless of
// whether it arrives as markdown link syntax or as raw HTML citationPlugin
// might walk into after rehypeSanitize has already run.

const mdLink = render("Click [1](javascript:alert(1)) to continue.", 3);
check(
  "markdown link [1](javascript:...) has its javascript: href neutralized",
  !mdLink.includes("javascript:"),
);
check(
  "markdown link [1](javascript:...) is not turned into a citation superscript",
  !mdLink.includes("tk-citation"),
);

const rawAnchor = render('<a href="javascript:alert(1)">[1]</a> is dangerous.', 3);
check(
  "raw <a href=javascript:...>[1]</a> has javascript: stripped end-to-end",
  !rawAnchor.includes("javascript:"),
);

if (checks.some(([, c]) => !c)) {
  console.error("check-citations FAILED");
  process.exit(1);
}
console.log("check-citations OK");
