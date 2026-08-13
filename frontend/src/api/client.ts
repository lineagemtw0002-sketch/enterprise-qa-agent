const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export interface DocumentInfo {
  id: string;
  filename: string;
  status: "pending" | "done" | "error";
  error: string | null;
  created_at: string;
}

export async function listDocuments(): Promise<DocumentInfo[]> {
  const res = await fetch(`${API_BASE}/api/documents`);
  if (!res.ok) throw new Error(`Failed to list documents: ${res.status}`);
  return res.json();
}

export async function uploadDocument(file: File): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_BASE}/api/documents`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

export type ChatEvent =
  | { type: "token"; content: string }
  | { type: "tool_result"; tool: string; content: string }
  | { type: "error"; message: string }
  | { type: "done" };

export async function* streamChat(message: string, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`Chat request failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sepIndex: number;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      const event = parseSseEvent(rawEvent);
      if (event) yield event;
    }
  }
}

function parseSseEvent(raw: string): ChatEvent | null {
  let eventType = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  return { type: eventType, ...JSON.parse(data) } as ChatEvent;
}
