import { useEffect, useRef, useState } from "react";
import { initialSimulate, sendChat } from "./api.js";

// A brand-new session id is created every time the page loads (assignment requirement).
const newSessionId = () => `s-${crypto.randomUUID()}`;

function Metadata({ meta }) {
  const fromCache = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span className={`badge ${fromCache ? "badge-cache" : "badge-live"}`} data-testid="meta-source">
        {meta.resolution_source}
      </span>
      <span className="meta-item">
        <span className="meta-label">model</span> <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="meta-item">
        <span className="meta-label">latency</span> <span data-testid="meta-latency">{meta.latency_ms} ms</span>
      </span>
      <span className="meta-item">
        <span className="meta-label">input tokens</span> <span data-testid="meta-input-tokens">{meta.input_tokens}</span>
      </span>
      <span className="meta-item">
        <span className="meta-label">output tokens</span> <span data-testid="meta-output-tokens">{meta.output_tokens}</span>
      </span>
      <span className="meta-item meta-muted">
        {meta.key_id ? `${meta.key_id} · ${meta.fallback_attempts} fallback${meta.fallback_attempts === 1 ? "" : "s"}` : "no key used"}
      </span>
    </div>
  );
}

function Message({ message }) {
  if (message.role === "user") {
    return (
      <div className="row row-user">
        <div className="bubble bubble-user" data-testid="user-message">{message.content}</div>
      </div>
    );
  }
  if (message.role === "error") {
    return (
      <div className="row">
        <div className="bubble bubble-error" role="alert" data-testid="error-message">{message.content}</div>
      </div>
    );
  }
  return (
    <div className="row">
      <div className="bubble bubble-assistant" data-testid="assistant-message">
        <div className="reply" data-testid="assistant-reply">{message.content}</div>
        <Metadata meta={message.metadata} />
      </div>
    </div>
  );
}

export default function App() {
  const [sessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [simulate, setSimulate] = useState(initialSimulate);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  async function send() {
    const text = input.trim();
    if (!text || loading) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    setInput(""); // chat-input is empty right after sending
    setLoading(true);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulate });
      setMessages((m) => [...m, { role: "assistant", content: data.reply, metadata: data.metadata }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", content: err.message }]);
    } finally {
      setLoading(false);
    }
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Lyftr Chat</h1>
        <label className="simulate" title="Demo only: sends the X-Simulate-Failure header">
          Simulate failure{" "}
          <select data-testid="simulate-select" value={simulate} onChange={(e) => setSimulate(e.target.value)}>
            <option value="">off</option>
            <option value="key_1=429">key_1 fails (429)</option>
            <option value="*=429">all keys fail</option>
            {!["", "key_1=429", "*=429"].includes(simulate) && <option value={simulate}>{simulate}</option>}
          </select>
        </label>
        <span className="session" title="A new session starts on every page load">session {sessionId.slice(0, 10)}…</span>
      </header>

      <div className="system-prompt">
        <label htmlFor="system-prompt-input">System prompt (optional)</label>
        <textarea
          id="system-prompt-input"
          data-testid="system-prompt-input"
          rows={2}
          placeholder="e.g. You are a pirate. Answer in one sentence."
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
      </div>

      <main className="chat" aria-live="polite">
        {messages.length === 0 && !loading && (
          <p className="empty">Ask something to get started. Under every answer you'll see where it came from, the model, latency and token usage.</p>
        )}
        {messages.map((m, i) => (
          <Message key={i} message={m} />
        ))}
        {loading && (
          <div className="row" data-testid="loading-indicator">
            <div className="bubble bubble-assistant typing" aria-label="Assistant is typing">
              <span /><span /><span />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <textarea
          data-testid="chat-input"
          rows={1}
          placeholder="Type a message… (Enter to send, Shift+Enter for a new line)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          aria-label="Message"
        />
        <button type="submit" data-testid="send-button" disabled={loading || !input.trim()}>
          {loading ? "Sending…" : "Send"}
        </button>
      </form>
    </div>
  );
}
