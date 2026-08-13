import { useRef, useState } from "react";
import { streamChat } from "../api/client";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls: { tool: string; content: string }[];
  error?: string;
}

export function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  async function send() {
    const text = input.trim();
    if (!text || isStreaming) return;

    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text, toolCalls: [] },
      { role: "assistant", content: "", toolCalls: [] },
    ]);
    setIsStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const event of streamChat(text, controller.signal)) {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (event.type === "token") {
            next[next.length - 1] = { ...last, content: last.content + event.content };
          } else if (event.type === "tool_result") {
            next[next.length - 1] = {
              ...last,
              toolCalls: [...last.toolCalls, { tool: event.tool, content: event.content }],
            };
          } else if (event.type === "error") {
            next[next.length - 1] = { ...last, error: event.message };
          }
          return next;
        });
      }
    } catch (err) {
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        next[next.length - 1] = { ...last, error: (err as Error).message };
        return next;
      });
    } finally {
      setIsStreaming(false);
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="chat-empty">向企业知识库或业务数据提问，例如"Acme Corp 一共有多少笔订单？"</div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`chat-bubble chat-bubble--${msg.role}`}>
            {msg.toolCalls.map((tc, j) => (
              <details key={j} className="tool-call">
                <summary>调用工具：{tc.tool}</summary>
                <pre>{tc.content}</pre>
              </details>
            ))}
            <div className="chat-bubble-content">{msg.content || (isStreaming && i === messages.length - 1 ? "…" : "")}</div>
            {msg.error && <div className="chat-error">出错：{msg.error}</div>}
          </div>
        ))}
      </div>

      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入问题…"
          disabled={isStreaming}
        />
        <button type="submit" disabled={isStreaming || !input.trim()}>
          发送
        </button>
      </form>
    </div>
  );
}
