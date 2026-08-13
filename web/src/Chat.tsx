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
const sanitizeSchema = {
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
  | { type: "thinking"; text: string }
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

// A turn's reasoning trace is an ordered timeline of thinking steps and tool
// calls (interleaved as the agent produces them). Thinking is present only
// when the model exposes readable reasoning; otherwise the trace holds tool
// calls alone and renders exactly as the pre-thinking tool trace did.
type ToolCall = { kind: "tool"; name: string; input: Record<string, unknown>; done: boolean };
type ThinkingStep = { kind: "thinking"; text: string };
type TraceItem = ToolCall | ThinkingStep;

type Turn = {
  role: "user" | "assistant";
  content: string;
  trace: TraceItem[];
  sources: SourceEventPayload[];
  status: "streaming" | "done" | "error";
  startedAt: number;
  elapsedMs: number | null;
  traceExpanded: boolean;
};

function newUserTurn(content: string): Turn {
  return {
    role: "user", content, trace: [], sources: [], status: "done",
    startedAt: Date.now(), elapsedMs: null, traceExpanded: false,
  };
}
function newAssistantTurn(): Turn {
  return {
    role: "assistant", content: "", trace: [], sources: [], status: "streaming",
    startedAt: Date.now(), elapsedMs: null, traceExpanded: true,
  };
}

const SUGGESTED_QUESTIONS = [
  { question: "How do materialized view checkpoints work?", meta: "architecture · proton" },
  { question: "What does the Helm chart set for timeplusd resources?", meta: "deployment · helm-charts" },
  { question: "How do I create an external stream to read from Kafka?", meta: "usage · docs" },
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

function LightbulbIcon() {
  return (
    <svg className="tk-think-bulb" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3a5.5 5.5 0 0 1 5.5 5.5c0 1.6-.7 2.9-1.6 4-.7.9-1.1 1.7-1.1 2.7v.8h-5.6v-.8c0-1-.4-1.8-1.1-2.7-.9-1.1-1.6-2.4-1.6-4A5.5 5.5 0 0 1 12 3z" />
      <path d="M9.8 19.5h4.4M10.6 21.5h2.8" />
    </svg>
  );
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
    <div className="tk-source-card">
      <div className="tk-source-card-header">
        <div className="tk-source-index">{n}.</div>
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
  const [agentModel, setAgentModel] = useState<string | null>(null);
  // The Sources panel is opened explicitly via each answer's "Sources (N)"
  // button (not automatically, and not tied to any inline citation number —
  // the model's citation text is unreliable). `activeSourcesTurn` is the turn
  // index whose read sources the panel shows.
  const [panelOpen, setPanelOpen] = useState(false);
  const [activeSourcesTurn, setActiveSourcesTurn] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

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

  // The model the chat agent runs on, shown in the header / empty state.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiFetch("/chat/model");
        if (!resp.ok) return;
        const data = await resp.json();
        if (!cancelled && data && typeof data.model === "string") setAgentModel(data.model);
      } catch {
        // Omit the model chip on failure rather than erroring.
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
    setActiveSourcesTurn(null);
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((ts) => [...ts, newUserTurn(message), newAssistantTurn()]);

    const update = (fn: (t: Turn) => Turn) =>
      setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);
    const finish = (status: "done" | "error") =>
      update((t) => ({
        ...t,
        status,
        elapsedMs: Date.now() - t.startedAt,
        // Leave the trace in whatever expand state it's in (expanded by
        // default) rather than auto-collapsing on done — collapsing a tall
        // panel to one line the instant the answer appears makes the UI jump.
        // The user collapses it manually via the header toggle.
        trace: t.trace.map((it) => (it.kind === "tool" ? { ...it, done: true } : it)),
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
        } else if (ev.type === "thinking") {
          update((t) => {
            // Consecutive thinking deltas coalesce into the current step; a
            // tool call between them closes it, so the next delta starts a
            // fresh step (interleaved thinking, per the mockup).
            const trace = [...t.trace];
            const last = trace[trace.length - 1];
            if (last && last.kind === "thinking") {
              trace[trace.length - 1] = { ...last, text: last.text + ev.text };
            } else {
              trace.push({ kind: "thinking", text: ev.text });
            }
            return { ...t, trace };
          });
        } else if (ev.type === "tool") {
          update((t) => {
            // Text streamed before a tool call is the model's between-step
            // narration (the turn continued into a tool call), not the
            // answer. Move it into the trace as a reasoning step and clear
            // `content`, so it survives the `done` handler replacing
            // `content` with the final turn's answer — otherwise this
            // narration is shown mid-stream and then vanishes. On endpoints
            // that redact extended thinking, this narration is the only
            // visible reasoning.
            const trace: TraceItem[] = t.content.trim()
              ? [...t.trace, { kind: "thinking", text: t.content }]
              : [...t.trace];
            // A new tool call implies any still-running tool finished (the
            // backend emits an explicit end only for read_source, via the
            // "source" event below); agent tools run sequentially.
            const withDone = trace.map((it) =>
              it.kind === "tool" && !it.done ? { ...it, done: true } : it);
            withDone.push({ kind: "tool", name: ev.name, input: ev.input ?? {}, done: false });
            return { ...t, trace: withDone, content: "" };
          });
        } else if (ev.type === "source") {
          update((t) => {
            const trace = t.trace.map((it) =>
              it.kind === "tool" && it.name === "read_source" && !it.done &&
              it.input.repo === ev.repo && it.input.file_path === ev.file_path &&
              Number(it.input.line_start) === ev.line_start
                ? { ...it, done: true }
                : it);
            return { ...t, trace, sources: [...t.sources, ev] };
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
    setActiveSourcesTurn(null);
  }

  function renderTrace(turn: Turn, idx: number) {
    if (turn.trace.length === 0) return null;
    const streaming = turn.status === "streaming";
    const elapsedMs = turn.elapsedMs ?? Date.now() - turn.startedAt;
    const thinkingCount = turn.trace.filter((it) => it.kind === "thinking").length;
    const toolCount = turn.trace.filter((it) => it.kind === "tool").length;
    const hasThinking = thinkingCount > 0;
    // With thinking the trace is collapsible even mid-stream (so `expanded`
    // follows traceExpanded alone); the tool-only trace stays locked open
    // while streaming, exactly as before.
    const expanded = hasThinking ? turn.traceExpanded : streaming || turn.traceExpanded;
    const toolLabel = `${toolCount} tool call${toolCount === 1 ? "" : "s"}`;
    return (
      <div className={hasThinking && !expanded ? "tk-trace tk-trace-collapsed" : "tk-trace"}>
        <button
          type="button"
          className="tk-trace-header"
          // With thinking, the header is always togglable (hide/show, even
          // mid-stream); the tool-only trace stays locked open while working.
          disabled={streaming && !hasThinking}
          onClick={() => toggleTrace(idx)}
        >
          {hasThinking && (streaming
            ? <span className="tk-think-spinner" aria-hidden="true" />
            : <LightbulbIcon />)}
          <span className={hasThinking && streaming ? "tk-trace-count tk-think-label" : "tk-trace-count"}>
            {hasThinking
              ? (streaming ? "Thinking" : `Thought for ${Math.round(elapsedMs / 1000)}s`)
              : (streaming ? "Working" : toolLabel)}
          </span>
          {hasThinking && !streaming && (
            <span className="tk-trace-summary">
              {thinkingCount} thinking step{thinkingCount === 1 ? "" : "s"} · {toolLabel}
            </span>
          )}
          {(!hasThinking || streaming) && (
            <span className="tk-trace-elapsed">{fmtElapsed(elapsedMs)}</span>
          )}
          {/* The summary already fills the row in thinking-done; otherwise a
              spacer pushes the toggle to the right edge. */}
          {!(hasThinking && !streaming) && <span className="tk-trace-spacer" />}
          {(hasThinking || !streaming) && (
            <span className="tk-trace-toggle">
              {hasThinking
                ? (expanded ? "hide ▾" : "show ▸")
                : (expanded ? "collapse ▾" : "expand ▸")}
            </span>
          )}
        </button>
        {expanded && (
          <div className="tk-trace-rows">
            {turn.trace.map((it, i) =>
              it.kind === "thinking" ? (
                <div className="tk-think-block" key={i}>
                  {it.text}
                  {streaming && i === turn.trace.length - 1 && <span className="tk-caret" />}
                </div>
              ) : (
                <div className="tk-trace-row" key={i}>
                  <span className={it.done ? "tk-trace-dot" : "tk-trace-dot pending"} />
                  <span className="tk-trace-name">{it.name}</span>
                  <span className="tk-trace-detail">{summarizeToolInput(it.name, it.input)}</span>
                  <span className={it.done ? "tk-trace-status" : "tk-trace-status running"}>
                    {it.done ? "" : "running…"}
                  </span>
                </div>
              ))}
          </div>
        )}
      </div>
    );
  }

  const hasTurns = turns.length > 0;
  const activeTurn = activeSourcesTurn != null ? turns[activeSourcesTurn] ?? null : null;
  const showSources = panelOpen && activeTurn != null && activeTurn.sources.length > 0;

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
          {agentModel && (
            <>
              <div className="tk-chat-topbar-label">model</div>
              <span className="tk-model-tag">{agentModel}</span>
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
          {agentModel && (
            <div className="tk-chat-empty-model">
              model <span className="tk-model-tag">{agentModel}</span>
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
          <div className="tk-chat-messages">
            <div className="tk-chat-messages-inner">
              {turns.map((turn, i) =>
                turn.role === "user" ? (
                  <div className="tk-msg tk-msg-user" key={i}>
                    <div className="tk-bubble-user">{turn.content}</div>
                  </div>
                ) : (
                  <div className="tk-msg tk-msg-assistant" key={i}>
                    {renderTrace(turn, i)}
                    {/* While streaming, the answer text is buffered in
                        `content` but not shown here — reasoning shows in the
                        trace and the answer appears complete once the turn is
                        done. A lone caret marks activity only before the trace
                        has anything to show (e.g. a direct, tool-less answer). */}
                    {turn.status !== "streaming" && turn.content ? (
                      <div className="tk-bubble-assistant">
                        {/* Raw HTML from the model is sanitized to a safe
                            subset (see sanitizeSchema) before rendering. */}
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
                        >
                          {turn.content}
                        </ReactMarkdown>
                      </div>
                    ) : turn.status === "streaming" && turn.trace.length === 0 ? (
                      <div className="tk-bubble-assistant"><span className="tk-caret" /></div>
                    ) : null}
                    {turn.status === "done" && turn.sources.length > 0 && (
                      <button
                        type="button"
                        className="tk-sources-btn"
                        onClick={() => { setActiveSourcesTurn(i); setPanelOpen(true); }}
                      >
                        Sources ({turn.sources.length})
                      </button>
                    )}
                  </div>
                ),
              )}
              <div ref={bottomRef} />
            </div>
          </div>
          {showSources && activeTurn && (
            <div className="tk-sources">
              <div className="tk-sources-header">
                <div className="tk-sources-title">Sources</div>
                <div className="tk-sources-count">{activeTurn.sources.length} read</div>
                <div className="tk-sources-spacer" />
                <button type="button" className="tk-sources-close" onClick={() => setPanelOpen(false)}>✕</button>
              </div>
              <div className="tk-sources-list">
                {activeTurn.sources.map((s) => <SourceCard key={s.n} n={s.n} source={s} />)}
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
