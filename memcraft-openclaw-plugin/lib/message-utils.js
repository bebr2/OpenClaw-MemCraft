function extractText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return "";
        if (typeof part.text === "string") return part.text;
        if (typeof part.content === "string") return part.content;
        return "";
      })
      .join("\n")
      .trim();
  }
  if (content && typeof content === "object") {
    if (typeof content.text === "string") return content.text;
    if (typeof content.content === "string") return content.content;
  }
  return "";
}

function stripMemoryContext(text) {
  if (!text) return "";
  // Remove injected memory block before persisting conversation memory.
  return text
    .replace(/<memory_context\b[^>]*>[\s\S]*?<\/memory_context>\s*/gi, "")
    .replace(/<memories\b[^>]*>[\s\S]*?<\/memories>\s*/gi, "")
    .trim();
}

function normalizeMessage(msg) {
  if (!msg || !msg.role) return null;
  let text = extractText(msg.content);
  if (msg.role === "user") {
    text = stripMemoryContext(text);
  }
  if (!text) return null;
  return { role: msg.role, content: text };
}

function isStartupBootstrapUserMessage(text) {
  const s = String(text || "").trim().toLowerCase();
  if (!s) return false;
  return (
    s.includes("a new session was started via /new or /reset") ||
    s.includes("execute your session startup sequence now")
  );
}

export function pickMessages(messages, cfg) {
  const normalized = (messages || []).map(normalizeMessage).filter(Boolean);

  return normalized.filter((m) => {
    if (m.role === "assistant") return cfg.includeAssistant;
    if (m.role === "tool") return cfg.includeToolMessages;
    if (m.role !== "user") return false;
    return !isStartupBootstrapUserMessage(m.content);
  });
}

export function pickPreviousTurnForAgentEnd(messages, cfg) {
  const normalized = (messages || []).map(normalizeMessage).filter(Boolean);
  if (!normalized.length) return [];

  let currentUserIdx = -1;
  for (let i = normalized.length - 1; i >= 0; i -= 1) {
    if (normalized[i].role === "user") {
      currentUserIdx = i;
      break;
    }
  }
  if (currentUserIdx <= 0) return [];

  let previousUserIdx = -1;
  for (let i = currentUserIdx - 1; i >= 0; i -= 1) {
    if (normalized[i].role === "user") {
      previousUserIdx = i;
      break;
    }
  }
  if (previousUserIdx < 0) return [];

  if (isStartupBootstrapUserMessage(normalized[previousUserIdx].content)) {
    return [];
  }

  const segment = normalized.slice(previousUserIdx, currentUserIdx);
  return segment.filter((m) => {
    if (m.role === "assistant") return cfg.includeAssistant;
    if (m.role === "tool") return cfg.includeToolMessages;
    if (m.role !== "user") return false;
    return !isStartupBootstrapUserMessage(m.content);
  });
}

export function pickLastCompletedTurnFromAgentEnd(messages, cfg) {
  const normalized = (messages || []).map(normalizeMessage).filter(Boolean);
  if (!normalized.length) return [];

  let lastUserIdx = -1;
  for (let i = normalized.length - 1; i >= 0; i -= 1) {
    if (normalized[i].role === "user") {
      if (!isStartupBootstrapUserMessage(normalized[i].content)) {
        lastUserIdx = i;
        break;
      }
    }
  }
  if (lastUserIdx < 0) return [];

  const segment = normalized.slice(lastUserIdx);
  return segment.filter((m) => {
    if (m.role === "assistant") return cfg.includeAssistant;
    if (m.role === "tool") return cfg.includeToolMessages;
    if (m.role !== "user") return false;
    return !isStartupBootstrapUserMessage(m.content);
  });
}
