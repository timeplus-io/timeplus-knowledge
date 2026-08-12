import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { apiFetch } from "./api";
import { readSource, type SourceResponse } from "./graph";

// Models emit <br> inside GFM table cells (cells cannot hold real
// newlines). rehype-raw parses raw HTML, then rehype-sanitize strips it
// back to a GitHub-grade safe subset: scripts, styles, iframes and all
// event-handler attributes are removed. We additionally drop img so model
// output can never load external resources (tracking pixels). Ported
// unchanged from the old App.tsx chat implementation.
// Exported (alongside citationPlugin below) so scripts/check-citations.mjs
// can render the EXACT rehypePlugins pipeline this component uses, instead
// of testing a copy that could silently drift from the real code.
export const sanitizeSchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== "img"),
};

// --------------------------------------------------------------------
// /chat SSE stream
// --------------------------------------------------------------------

type SourceEventPayload = {
  n: number;
  repo: string;
  file_path: string;
  line_start: number;
  line_end: number;
};

type ChatEvent =
  | { type: "token"; text: string }
  | { type: "tool"; name: string; input: Record<string, unknown> }
  | ({ type: "source" } & SourceEventPayload)
  | { type: "done"; text: string; sources: SourceEventPayload[] }
  | { type: "error"; message: string };

async function* sseEvents(resp: Response): AsyncGenerator<ChatEvent> {
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

// --------------------------------------------------------------------
// Turn state
// --------------------------------------------------------------------

type ToolCall = { name: string; input: Record<string, unknown>; done: boolean };

type Turn = {
  role: "user" | "assistant";
  content: string;
  tools: ToolCall[];
  sources: SourceEventPayload[];
  status: "streaming" | "done" | "error";
  startedAt: number;
  elapsedMs: number | null;
  traceExpanded: boolean;
};

function newUserTurn(content: string): Turn {
  return {
    role: "user", content, tools: [], sources: [], status: "done",
    startedAt: Date.now(), elapsedMs: null, traceExpanded: false,
  };
}
function newAssistantTurn(): Turn {
  return {
    role: "assistant", content: "", tools: [], sources: [], status: "streaming",
    startedAt: Date.now(), elapsedMs: null, traceExpanded: true,
  };
}

const SUGGESTED_QUESTIONS = [
  { question: "How do materialized view checkpoints work?", meta: "architecture · proton" },
  { question: "What does the Helm chart set for timeplusd resources?", meta: "deployment · helm-charts" },
  { question: "Which CLI command creates an external stream?", meta: "usage · timeplus-cli" },
  { question: "Trace the call path from HTTP insert to nativelog write", meta: "code path · proton" },
];

// Renders a tool call's input args as a compact one-line summary for the
// trace row (e.g. `query="checkpoint" kind=any`, `path=src/x.cpp
// lines=118-142`). Generic key=value fallback covers every current and
// future tool without hardcoding each one.
function summarizeToolInput(name: string, input: Record<string, unknown>): string {
  if (name === "read_source" && typeof input.file_path === "string") {
    const parts = [`path=${input.file_path}`];
    if (input.line_start != null && input.line_end != null) {
      parts.push(`lines=${input.line_start}-${input.line_end}`);
    }
    return parts.join(" ");
  }
  return Object.entries(input)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      if (Array.isArray(v)) return `${k}=${v.join(",")}`;
      return `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`;
    })
    .join(" ");
}

function fmtElapsed(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

// --------------------------------------------------------------------
// Citation superscripts — [n] -> <sup><a href="#tk-source-n">[n]</a></sup>
//
// This is the LAST rehype plugin in the pipeline, running strictly after
// rehypeRaw + rehypeSanitize have already reduced the model's raw markdown
// to the safe subset (sanitizeSchema, unmodified). It never re-parses
// untrusted text as HTML — it only ever constructs `sup`/`a` hast element
// nodes itself (both already allowed by defaultSchema) around a digit-only
// `[n]` match, so it cannot reintroduce anything sanitize would strip. The
// hast tree's node shape isn't in this project's dependency graph as a
// typed package, hence the `any`s confined to this one plugin.
//
// Exported for scripts/check-citations.mjs (see sanitizeSchema above for
// why: the permanent XSS-safety test must exercise this real function).
export function citationPlugin(sourceCount: number) {
  return function transformer(tree: any) {
    if (sourceCount <= 0) return;
    walk(tree);
  };

  function walk(node: any) {
    if (!node.children || node.children.length === 0) return;
    // Never rewrite inside code/pre — "[1]" there is code, not a citation.
    if (node.tagName === "code" || node.tagName === "pre") return;
    const next: any[] = [];
    for (const child of node.children) {
      if (child.type === "text" && typeof child.value === "string" && /\[\d+\]/.test(child.value)) {
        next.push(...splitText(child.value));
      } else {
        walk(child);
        next.push(child);
      }
    }
    node.children = next;
  }

  function splitText(text: string): any[] {
    const out: any[] = [];
    const re = /\[(\d+)\]/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push({ type: "text", value: text.slice(last, m.index) });
      const n = Number(m[1]);
      if (n >= 1 && n <= sourceCount) {
        out.push({
          type: "element",
          tagName: "sup",
          properties: {},
          children: [{
            type: "element",
            tagName: "a",
            properties: { href: `#tk-source-${n}`, className: ["tk-citation"] },
            children: [{ type: "text", value: `[${n}]` }],
          }],
        });
      } else {
        out.push({ type: "text", value: m[0] });
      }
      last = re.lastIndex;
    }
    if (last < text.length) out.push({ type: "text", value: text.slice(last) });
    return out;
  }
}

// --------------------------------------------------------------------
// Source card — lazily fetches its own code preview via
// GET /api/graph/source (graph.ts's readSource) once mounted, i.e. only
// once the Sources panel actually renders it, not eagerly for every
// citation while the answer is still streaming.
// --------------------------------------------------------------------

function SourceCard({ n, source }: { n: number; source: SourceEventPayload }) {
  const [preview, setPreview] = useState<SourceResponse | "loading" | "error">("loading");

  useEffect(() => {
    let cancelled = false;
    setPreview("loading");
    readSource(source.repo, source.file_path, source.line_start, source.line_end)
      .then((r) => { if (!cancelled) setPreview(r); })
      .catch(() => { if (!cancelled) setPreview("error"); });
    return () => { cancelled = true; };
  }, [source.repo, source.file_path, source.line_start, source.line_end]);

  return (
    <div className="tk-source-card" id={`tk-source-${n}`}>
      <div className="tk-source-card-header">
        <div className="tk-source-index">[{n}]</div>
        <div className="tk-source-path">{source.file_path}</div>
      </div>
      {preview === "loading" ? (
        <div className="tk-source-preview-status">Loading preview…</div>
      ) : preview === "error" ? (
        <div className="tk-source-preview-status">Preview unavailable</div>
      ) : (
        <div className="tk-source-preview">
          {preview.lines.map((line, i) => (
            <div className="tk-source-line" key={preview.line_start + i}>
              <span className="tk-source-lineno">{preview.line_start + i}</span>
              <span>{line}</span>
            </div>
          ))}
        </div>
      )}
      <div className="tk-source-card-footer">
        {source.repo} · lines {source.line_start}–{source.line_end}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------
// Chat
// --------------------------------------------------------------------

export default function Chat({
  initialInput,
  onConsumeInitial,
}: {
  // Task 5 (Explorer "Ask about this") will pass an initial question in and
  // get notified once Chat has consumed it into its input box.
  initialInput?: string;
  onConsumeInitial?: () => void;
} = {}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [corpusTags, setCorpusTags] = useState<string[]>([]);
  // The Sources panel opens only when the reader clicks an inline [n]
  // citation (not automatically). `scrollToSource` scrolls to the matching
  // card once the panel has rendered.
  const [panelOpen, setPanelOpen] = useState(false);
  const [scrollToSource, setScrollToSource] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollToSource == null) return;
    document.getElementById(`tk-source-${scrollToSource}`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
    setScrollToSource(null);
  }, [scrollToSource, panelOpen]);

  // Delegated click on the message area: a [n] citation link (rendered by
  // citationPlugin as <a class="tk-citation" href="#tk-source-N">) opens the
  // Sources panel and scrolls to card N, instead of the default hash jump.
  function onMessagesClick(e: React.MouseEvent) {
    const a = (e.target as HTMLElement).closest("a.tk-citation") as HTMLAnchorElement | null;
    if (!a) return;
    e.preventDefault();
    const n = Number(a.getAttribute("href")?.replace("#tk-source-", ""));
    if (!Number.isNaN(n)) {
      setPanelOpen(true);
      setScrollToSource(n);
    }
  }

  useEffect(() => {
    if (initialInput) {
      setInput(initialInput);
      onConsumeInitial?.();
    }
    // Only re-run when a new initialInput value arrives; onConsumeInitial is
    // a stable-enough callback that including it would just re-fire this on
    // every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialInput]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // GET /api/repos is admin-only today (src/tpk/api.py's
        // require_admin). Rather than special-case the caller's role here,
        // treat any non-ok response (403 for a non-admin, or anything else)
        // the same as "no corpus tags to show" -- the empty state and top
        // bar simply omit the row instead of surfacing an error.
        const resp = await apiFetch("/api/repos");
        if (!resp.ok) return;
        const data = await resp.json();
        if (cancelled || !Array.isArray(data)) return;
        setCorpusTags(
          data
            .filter((r: { enabled?: boolean }) => r.enabled)
            .map((r: { entry_key: string }) => r.entry_key),
        );
      } catch {
        // Network failure -- omit the tags row rather than erroring.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function send(overrideMessage?: string) {
    const message = (overrideMessage ?? input).trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setPanelOpen(false);
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((ts) => [...ts, newUserTurn(message), newAssistantTurn()]);

    const update = (fn: (t: Turn) => Turn) =>
      setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);
    const finish = (status: "done" | "error") =>
      update((t) => ({
        ...t,
        status,
        elapsedMs: Date.now() - t.startedAt,
        traceExpanded: false,
        tools: t.tools.map((tc) => ({ ...tc, done: true })),
      }));

    try {
      // A 403 password_change_required here is handled centrally by
      // api.ts's onPasswordChangeRequired hook (registered in App.tsx).
      const resp = await apiFetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      if (!resp.ok || !resp.body) {
        update((t) => ({ ...t, content: t.content + `\n\n[error] HTTP ${resp.status}` }));
        finish("error");
        return;
      }
      for await (const ev of sseEvents(resp)) {
        if (ev.type === "token") {
          update((t) => ({ ...t, content: t.content + ev.text }));
        } else if (ev.type === "tool") {
          update((t) => {
            // The previously-last tool call is implicitly done once a new
            // one starts (the backend doesn't emit an explicit tool-end
            // event for every tool -- only read_source's is surfaced, via
            // the "source" event handled below).
            const tools = t.tools.map((tc, i) =>
              i === t.tools.length - 1 ? { ...tc, done: true } : tc);
            tools.push({ name: ev.name, input: ev.input ?? {}, done: false });
            return { ...t, tools };
          });
        } else if (ev.type === "source") {
          update((t) => {
            const tools = t.tools.map((tc) =>
              tc.name === "read_source" && !tc.done &&
              tc.input.repo === ev.repo && tc.input.file_path === ev.file_path &&
              Number(tc.input.line_start) === ev.line_start
                ? { ...tc, done: true }
                : tc);
            return { ...t, tools, sources: [...t.sources, ev] };
          });
        } else if (ev.type === "done") {
          update((t) => ({ ...t, content: ev.text, sources: ev.sources ?? t.sources }));
          finish("done");
        } else if (ev.type === "error") {
          update((t) => ({ ...t, content: t.content + `\n\n[error] ${ev.message}` }));
          finish("error");
        }
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    } catch (e) {
      update((t) => ({ ...t, content: t.content + `\n\n[error] ${String(e)}` }));
      finish("error");
    } finally {
      setBusy(false);
    }
  }

  function toggleTrace(idx: number) {
    setTurns((ts) => ts.map((t, i) => (i === idx ? { ...t, traceExpanded: !t.traceExpanded } : t)));
  }

  // Clear the conversation and return to the empty state. Disabled while a
  // response is streaming (like the input) so a lingering stream can't write
  // into a cleared/new conversation; mid-stream abort is future work (#10).
  function newConversation() {
    setTurns([]);
    setInput("");
    setPanelOpen(false);
  }

  function renderTrace(turn: Turn, idx: number) {
    if (turn.tools.length === 0) return null;
    const streaming = turn.status === "streaming";
    const elapsedMs = turn.elapsedMs ?? Date.now() - turn.startedAt;
    return (
      <div className="tk-trace">
        <button
          type="button"
          className="tk-trace-header"
          disabled={streaming}
          onClick={() => toggleTrace(idx)}
        >
          <span className="tk-trace-count">
            {streaming ? "Working" : `${turn.tools.length} tool call${turn.tools.length === 1 ? "" : "s"}`}
          </span>
          <span className="tk-trace-elapsed">{fmtElapsed(elapsedMs)}</span>
          <span className="tk-trace-spacer" />
          {!streaming && (
            <span className="tk-trace-toggle">{turn.traceExpanded ? "collapse ▾" : "expand ▸"}</span>
          )}
        </button>
        {(streaming || turn.traceExpanded) && (
          <div className="tk-trace-rows">
            {turn.tools.map((tc, i) => (
              <div className="tk-trace-row" key={i}>
                <span className={tc.done ? "tk-trace-dot" : "tk-trace-dot pending"} />
                <span className="tk-trace-name">{tc.name}</span>
                <span className="tk-trace-detail">{summarizeToolInput(tc.name, tc.input)}</span>
                <span className={tc.done ? "tk-trace-status" : "tk-trace-status running"}>
                  {tc.done ? "" : "running…"}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  const hasTurns = turns.length > 0;
  const answeredWithSources = turns.filter(
    (t) => t.role === "assistant" && t.status === "done" && t.sources.length > 0);
  const sourcesTurn = answeredWithSources.length
    ? answeredWithSources[answeredWithSources.length - 1]
    : null;
  const showSources = panelOpen && sourcesTurn !== null;

  return (
    <div className="tk-chat">
      {hasTurns && (
        <div className="tk-chat-topbar">
          <div className="tk-chat-topbar-title">Chat</div>
          <div className="tk-chat-topbar-spacer" />
          {corpusTags.length > 0 && (
            <>
              <div className="tk-chat-topbar-label">answering from</div>
              <div className="tk-corpus-tags">
                {corpusTags.map((tag) => <span className="tk-corpus-tag" key={tag}>{tag}</span>)}
              </div>
            </>
          )}
        </div>
      )}

      {!hasTurns ? (
        <div className="tk-chat-empty">
          <div className="tk-chat-empty-heading">
            <div className="tk-chat-empty-title">Ask anything about Timeplus</div>
            <div className="tk-chat-empty-subtitle">
              Answers are grounded in the indexed corpus and cite the source they read.
            </div>
          </div>
          {corpusTags.length > 0 && (
            <div className="tk-corpus-tags">
              {corpusTags.map((tag) => <span className="tk-corpus-tag" key={tag}>{tag}</span>)}
            </div>
          )}
          <div className="tk-suggested-grid">
            {SUGGESTED_QUESTIONS.map((q) => (
              <button
                key={q.question}
                type="button"
                className="tk-suggested-card"
                onClick={() => send(q.question)}
              >
                <div className="tk-suggested-card-title">{q.question}</div>
                <div className="tk-suggested-card-meta">{q.meta}</div>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="tk-chat-body">
          <div className="tk-chat-messages" onClick={onMessagesClick}>
            <div className="tk-chat-messages-inner">
              {turns.map((turn, i) =>
                turn.role === "user" ? (
                  <div className="tk-msg tk-msg-user" key={i}>
                    <div className="tk-bubble-user">{turn.content}</div>
                  </div>
                ) : (
                  <div className="tk-msg tk-msg-assistant" key={i}>
                    {renderTrace(turn, i)}
                    <div className="tk-bubble-assistant">
                      {turn.content ? (
                        <>
                          {/* Raw HTML from the model is sanitized to a safe
                              subset (see sanitizeSchema above) before the
                              citation plugin ever runs. */}
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                            rehypePlugins={[
                              rehypeRaw,
                              [rehypeSanitize, sanitizeSchema],
                              [citationPlugin, turn.sources.length],
                            ]}
                          >
                            {turn.content}
                          </ReactMarkdown>
                          {turn.status === "streaming" && <span className="tk-caret" />}
                        </>
                      ) : turn.status === "streaming" ? (
                        <span className="tk-caret" />
                      ) : null}
                    </div>
                  </div>
                ),
              )}
              <div ref={bottomRef} />
            </div>
          </div>
          {showSources && sourcesTurn && (
            <div className="tk-sources">
              <div className="tk-sources-header">
                <div className="tk-sources-title">Sources</div>
                <div className="tk-sources-count">{sourcesTurn.sources.length} cited</div>
                <div className="tk-sources-spacer" />
                <button type="button" className="tk-sources-close" onClick={() => setPanelOpen(false)}>✕</button>
              </div>
              <div className="tk-sources-list">
                {sourcesTurn.sources.map((s) => <SourceCard key={s.n} n={s.n} source={s} />)}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="tk-chat-footer">
        <div className="tk-chat-footer-inner">
          <textarea
            value={input}
            placeholder={hasTurns ? "Ask a follow-up…" : "e.g. How do materialized view checkpoints work?"}
            disabled={busy}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
          />
          <button type="button" onClick={() => send()} disabled={busy || !input.trim()}>
            {busy ? "Thinking…" : "Send"}
          </button>
          {hasTurns && (
            <button
              type="button"
              className="secondary"
              onClick={newConversation}
              disabled={busy}
              title="Clear this conversation and start a new one"
            >
              New chat
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
