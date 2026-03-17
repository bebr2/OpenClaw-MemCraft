const toBool = (v, fallback = false) => {
  if (v === undefined || v === null || v === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(v).toLowerCase());
};

const toInt = (v, fallback) => {
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) ? n : fallback;
};

const toCsvList = (v, fallback = []) => {
  if (v === undefined || v === null || v === "") return fallback;
  return String(v)
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
};

const toStoreGranularity = (v, fallback = "session_end") => {
  if (v === undefined || v === null || v === "") return fallback;
  const s = String(v).trim().toLowerCase();
  if (s === "session_end" || s === "session") return "session_end";
  if (
    s === "agent_end" ||
    s === "main_agent_end" ||
    s === "main_agent_run" ||
    s === "run"
  ) {
    return "agent_end";
  }
  return fallback;
};

export function buildConfig(pluginConfig = {}) {
  const env = process.env;
  const timeoutMs = toInt(pluginConfig.timeoutMs || env.MEMCRAFT_TIMEOUT_MS, 100000);

  return {
    serverUrl: String(
      pluginConfig.serverUrl || env.MEMCRAFT_SERVER_URL || "http://127.0.0.1:8765",
    ).replace(/\/+$/, ""),
    timeoutMs,
    retrieveTimeoutMs: toInt(
      pluginConfig.retrieveTimeoutMs || env.MEMCRAFT_RETRIEVE_TIMEOUT_MS,
      timeoutMs,
    ),
    storeTimeoutMs: toInt(
      pluginConfig.storeTimeoutMs || env.MEMCRAFT_STORE_TIMEOUT_MS,
      Math.max(timeoutMs, 450000),
    ),
    topK: toInt(pluginConfig.topK || env.MEMCRAFT_TOP_K, 5),
    includeAssistant: toBool(
      pluginConfig.includeAssistant ?? env.MEMCRAFT_INCLUDE_ASSISTANT,
      true,
    ),
    includeToolMessages: toBool(
      pluginConfig.includeToolMessages ?? env.MEMCRAFT_INCLUDE_TOOL_MESSAGES,
      false,
    ),
    promptTitle: String(
      pluginConfig.promptTitle || env.MEMCRAFT_PROMPT_TITLE || "Related memory context",
    ),
    namespace: String(pluginConfig.namespace || env.MEMCRAFT_NAMESPACE || "default"),
    excludeAgentIds: toCsvList(
      pluginConfig.excludeAgentIds ?? env.MEMCRAFT_EXCLUDE_AGENT_IDS,
      ["memory-compressor"],
    ),
    retrieveBySession: toBool(
      pluginConfig.retrieveBySession ?? env.MEMCRAFT_RETRIEVE_BY_SESSION,
      false,
    ),
    debug: toBool(pluginConfig.debug ?? env.MEMCRAFT_DEBUG, false),
    logFinalContext: toBool(
      pluginConfig.logFinalContext ?? env.MEMCRAFT_LOG_FINAL_CONTEXT,
      false,
    ),
    contextPreviewMaxChars: toInt(
      pluginConfig.contextPreviewMaxChars ?? env.MEMCRAFT_CONTEXT_PREVIEW_MAX_CHARS,
      2000,
    ),
    stripHistoryMemory: toBool(
      pluginConfig.stripHistoryMemory ?? env.MEMCRAFT_STRIP_HISTORY_MEMORY,
      false,
    ),
    // Prefer env to let runtime switch mode without changing persisted plugin entries config.
    storeGranularity: toStoreGranularity(
      env.MEMCRAFT_STORE_GRANULARITY ?? pluginConfig.storeGranularity,
      "session_end",
    ),
  };
}
