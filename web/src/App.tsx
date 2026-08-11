import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Turn = { role: "user" | "assistant"; content: string; tools?: string[] };

async function* sseEvents(resp: Response): AsyncGenerator<any> {
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

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((ts) => [...ts, { role: "user", content: message }, { role: "assistant", content: "", tools: [] }]);

    const update = (fn: (t: Turn) => Turn) =>
      setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

    try {
      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      if (!resp.ok || !resp.body) {
        update((t) => ({ ...t, content: t.content + `\n\n[error] HTTP ${resp.status}` }));
        return;
      }
      for await (const ev of sseEvents(resp)) {
        if (ev.type === "token") update((t) => ({ ...t, content: t.content + ev.text }));
        else if (ev.type === "tool") update((t) => ({ ...t, tools: [...(t.tools ?? []), ev.name] }));
        else if (ev.type === "done") update((t) => ({ ...t, content: ev.text }));
        else if (ev.type === "error") update((t) => ({ ...t, content: t.content + `\n\n[error] ${ev.message}` }));
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    } catch (e) {
      update((t) => ({ ...t, content: t.content + `\n\n[error] ${String(e)}` }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Timeplus Knowledge</h1>
        <p>Ask anything about Timeplus — code, architecture, deployment.</p>
      </header>
      <main>
        {turns.map((t, i) => (
          <div key={i} className={`turn ${t.role}`}>
            {t.tools && t.tools.length > 0 && (
              <div className="tools">🔎 {t.tools.join(" → ")}</div>
            )}
            <div className="bubble">
              {t.role === "assistant" && t.content ? (
                // react-markdown renders React elements (HTML in the model
                // output is escaped, never injected), so this stays XSS-safe.
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{t.content}</ReactMarkdown>
              ) : (
                t.content || (busy && i === turns.length - 1 ? "…" : "")
              )}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </main>
      <footer>
        <textarea
          value={input}
          placeholder="e.g. How do materialized view checkpoints work?"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
        />
        <button onClick={send} disabled={busy || !input.trim()}>
          {busy ? "Thinking…" : "Send"}
        </button>
      </footer>
    </div>
  );
}
