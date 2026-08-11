// Renders the chat UI's markdown pipeline server-side and asserts the
// sanitization contract from issue #2:
//   1. <br> inside table cells renders as a line break
//   2. <script> and inline event handlers never reach the output
//   3. <img> is stripped (no external resource loading from model output)
// Run: npm run check:sanitize
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";

const sanitizeSchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== "img"),
};

function render(md) {
  return renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      {
        remarkPlugins: [remarkGfm],
        rehypePlugins: [rehypeRaw, [rehypeSanitize, sanitizeSchema]],
      },
      md,
    ),
  );
}

const checks = [];
function check(name, cond) {
  checks.push([name, cond]);
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
}

const table = render("| a | b |\n|---|---|\n| line1<br>line2 | x |");
check("<br> in table cell renders as <br/>", /line1<br\/?>line2/.test(table));
check("table still renders", table.includes("<table>"));

const evil = render(
  'hello <script>alert(1)</script> <img src="https://evil/px.gif" onerror="alert(2)"> <a href="javascript:alert(3)">x</a> world',
);
check("script tag stripped", !evil.includes("<script"));
check("script body not executable markup", !evil.includes("alert(1)</script>"));
check("img stripped", !evil.includes("<img"));
check("javascript: href neutralized", !evil.includes('href="javascript:'));

const iframe = render('<iframe src="https://evil"></iframe> <b onclick="x()">bold</b>');
check("iframe stripped", !iframe.includes("<iframe"));
check("event handler attribute stripped", !iframe.includes("onclick"));

if (checks.some(([, c]) => !c)) {
  console.error("sanitize-check FAILED");
  process.exit(1);
}
console.log("sanitize-check OK");
