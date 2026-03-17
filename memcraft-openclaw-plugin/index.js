#!/usr/bin/env node
import { buildConfig } from "./lib/config.js";
import { retrieveMemory, storeConversation } from "./lib/memory-client.js";
import { pickMessages, pickLastCompletedTurnFromAgentEnd } from "./lib/message-utils.js";

const skipNextRetrieveBySession = new Map();
let skipNextRetrieveGlobal = 0;
const pendingStoreBySession = new Map();
const pendingAgentEndStoreBySession = new Map();
const subagentSessionKeys = new Set();
const UUID_SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// Example matched prefix: [Tue 2026-03-10 20:10 GMT+8]
const QUERY_TIMESTAMP_PREFIX_RE =
  /^\s*\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[+-]\d{1,2}(?::?\d{2})?\]\s*/;

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

function extractQuery(event, cfg) {
  // Prefer current turn fields first; message history can lag one user turn.
  const direct = normalizeRetrieveQuery(event?.prompt, cfg);
  if (direct) return direct;

  const directAlt = normalizeRetrieveQuery(event?.query || event?.input, cfg);
  if (directAlt) return directAlt;

  const messages = Array.isArray(event?.messages) ? event.messages : [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m?.role !== "user") continue;
    const text = normalizeRetrieveQuery(extractText(m.content), cfg);
    if (text) return text;
  }

  return "";
}

function buildRetrieveMessages(event, cfg, query) {
  const sourceMessages = Array.isArray(event?.messages) ? event.messages : [];
  const picked = pickMessages(sourceMessages, cfg);
  if (!query) return picked;

  // Ensure backend sees current turn as the latest user message, even if history lags.
  const normalizedQuery = normalizeRetrieveQuery(query, cfg);
  const lastUser = [...picked].reverse().find((m) => m?.role === "user");
  const lastUserText = normalizeRetrieveQuery(lastUser?.content, cfg);
  if (lastUserText === normalizedQuery) return picked;
  return [...picked, { role: "user", content: normalizedQuery }];
}

function normalizeRetrieveQuery(raw, cfg) {
  const sourceText = String(raw || "");
  const text = cfg?.stripHistoryMemory ? stripMemoryContext(sourceText) : sourceText;
  const trimmed = text.trim();
  if (!trimmed) return "";
  if (cfg?.stripHistoryMemory) {
    return trimmed.replace(QUERY_TIMESTAMP_PREFIX_RE, "").trim();
  }
  // Even when keeping history memory blocks, strip transport-level timestamp prefix noise.
  return trimmed.replace(QUERY_TIMESTAMP_PREFIX_RE, "").trim();
}

function stripMemoryContext(text) {
  if (!text) return "";
  return text
    .replace(/<memory_context\b[^>]*>[\s\S]*?<\/memory_context>\s*/gi, "")
    .replace(/<memories\b[^>]*>[\s\S]*?<\/memories>\s*/gi, "")
    .trim();
}

function resolveSessionKey(ctx) {
  return String(ctx?.sessionId || ctx?.sessionKey || "").trim();
}

function normalizeSessionId(value) {
  const candidate = String(value || "").trim();
  if (!candidate) return null;
  return UUID_SESSION_ID_RE.test(candidate) ? candidate : null;
}

function resolveCanonicalSessionId(ctx) {
  return normalizeSessionId(ctx?.sessionId) || normalizeSessionId(ctx?.sessionKey);
}

function bump(map, key) {
  if (!key) return;
  map.set(key, (map.get(key) || 0) + 1);
}

function bumpGlobal(kind) {
  if (kind === "retrieve") skipNextRetrieveGlobal += 1;
}

function consume(map, key) {
  if (!key) return false;
  const count = map.get(key) || 0;
  if (count <= 0) return false;
  if (count === 1) {
    map.delete(key);
  } else {
    map.set(key, count - 1);
  }
  return true;
}

function consumeGlobal(kind) {
  if (kind === "retrieve") {
    if (skipNextRetrieveGlobal <= 0) return false;
    skipNextRetrieveGlobal -= 1;
    return true;
  }
  return false;
}

function isInternalProtocolQuery(query) {
  const s = String(query || "").trim().toLowerCase();
  if (!s) return false;
  return (
    s.includes("please output only valid json object") ||
    s.includes("return strict json only") ||
    s.includes("memory compression protocol")
  );
}

function isExcludedAgent(cfg, ctx) {
  const agentId = String(ctx?.agentId || "").trim().toLowerCase();
  if (!agentId) return false;
  return (cfg.excludeAgentIds || []).includes(agentId);
}

function isLikelyBeforeCompactionEvent(event) {
  if (!event || typeof event !== "object") return false;

  const hasSessionFile = typeof event.sessionFile === "string" && event.sessionFile.trim().length > 0;
  const compactingCount = Number(event.compactingCount);
  if (Number.isFinite(compactingCount) && compactingCount > 0) return true;

  const messageCount = Number(event.messageCount);
  const tokenCount = Number(event.tokenCount);
  if (hasSessionFile && Number.isFinite(messageCount) && messageCount > 0) return true;
  if (Number.isFinite(messageCount) && Number.isFinite(tokenCount) && messageCount > 0) return true;

  if (Array.isArray(event.messages) && event.messages.length > 0) return true;

  return false;
}

function buildMemoryBlock(title, memoryText) {
  if (!memoryText || !memoryText.trim()) return "";
  return [
    `<memories title="${title}">`,
    memoryText.trim(),
    "</memories>",
    "",
  ].join("\n");
}

function isSubagentSession(ctx) {
  const sessionKey = resolveSessionKey(ctx);
  if (!sessionKey) return false;
  return subagentSessionKeys.has(sessionKey);
}

export default {
  id: "memcraft-openclaw-plugin",
  name: "MemCraft Plugin",
  description: "Retrieve memory before prompt build, store compressed memory by configured mode",
  kind: "lifecycle",

  register(api) {
    const cfg = buildConfig(api.pluginConfig);
    const log = api.logger ?? console;

    if (cfg.debug) {
      log.info?.(
        `[memcraft] config store_granularity=${cfg.storeGranularity} include_assistant=${cfg.includeAssistant} include_tool_messages=${cfg.includeToolMessages}`,
      );
      log.info?.(`[memcraft] config strip_history_memory=${cfg.stripHistoryMemory}`);
      log.info?.(
        `[memcraft] config sources env_store_granularity=${process.env.MEMCRAFT_STORE_GRANULARITY || ""} plugin_store_granularity=${api.pluginConfig?.storeGranularity || ""}`,
      );
    }

    api.on("before_message_write", (event, ctx) => {
      if (!cfg.stripHistoryMemory) return;
      if (isExcludedAgent(cfg, ctx)) return;

      const message = event?.message;
      if (!message || message.role !== "user") return;
      if (typeof message.content !== "string") return;

      const sanitized = stripMemoryContext(message.content);
      if (!sanitized || sanitized === message.content) return;

      if (cfg.debug) {
        const sessionKey = resolveSessionKey(ctx);
        log.info?.(`[memcraft] before_message_write stripped_history_memory session=${sessionKey || "-"}`);
      }

      return {
        message: {
          ...message,
          content: sanitized,
        },
      };
    });

    const markSkipForMaintenanceTurn = (ctx, source) => {
      const sessionKey = resolveSessionKey(ctx);
      bump(skipNextRetrieveBySession, sessionKey);
      if (!sessionKey) {
        // Some lifecycle hooks may not carry a session id; keep a global fallback.
        bumpGlobal("retrieve");
      }
      if (cfg.debug) {
        log.info?.(`[memcraft] ${source} mark_skip session=${sessionKey || "-"}`);
      }
    };

    api.on("session_start", async (_event, ctx) => {
      if (isExcludedAgent(cfg, ctx)) return;
      // Skip startup handshake turn to avoid retrieving framework boot text.
      markSkipForMaintenanceTurn(ctx, "session_start");
    });

    api.on("before_compaction", async (event, ctx) => {
      if (isExcludedAgent(cfg, ctx)) return;

      const likelyCompaction = isLikelyBeforeCompactionEvent(event);

      if (likelyCompaction) {
        markSkipForMaintenanceTurn(ctx, "before_compaction");
      }
    });

    api.on("before_reset", async (_event, ctx) => {
      if (isExcludedAgent(cfg, ctx)) return;
      markSkipForMaintenanceTurn(ctx, "before_reset");
    });

    api.on("llm_input", async (event, ctx) => {
      if (!cfg.debug) return;
      if (isExcludedAgent(cfg, ctx)) return;
      const promptPreview = String(event?.prompt || "").slice(0, 1200);
      log.info?.(`[memcraft] llm_input prompt_preview=${promptPreview}`);
    });

    api.on("before_prompt_build", async (event, ctx) => {
      if (isExcludedAgent(cfg, ctx)) {
        if (cfg.debug) {
          log.info?.(`[memcraft] before_prompt_build skip excluded_agent=${ctx?.agentId || "-"}`);
        }
        return;
      }

      const sessionKey = resolveSessionKey(ctx);
      if (consume(skipNextRetrieveBySession, sessionKey) || consumeGlobal("retrieve")) {
        if (cfg.debug) {
          log.info?.(`[memcraft] before_prompt_build skip lifecycle_guard session=${sessionKey || "-"}`);
        }
        return;
      }

      if (cfg.storeGranularity === "agent_end") {
        const pendingTurnMessages = pendingAgentEndStoreBySession.get(sessionKey) || [];
        if (isSubagentSession(ctx)) {
          if (cfg.debug) {
            log.info?.(`[memcraft] before_prompt_build skip async_store subagent_session session=${sessionKey || "-"}`);
          }
        } else if (pendingTurnMessages.length) {
          pendingAgentEndStoreBySession.delete(sessionKey);
          const canonicalSessionId = resolveCanonicalSessionId(ctx);

          const payload = {
            session_id: canonicalSessionId,
            agent_id: ctx?.agentId || null,
            namespace: cfg.namespace,
            trigger: "before_prompt_build_agent_end",
            messages: pendingTurnMessages,
          };

          // Fire-and-forget to avoid blocking the current main session turn.
          void storeConversation(cfg, payload)
            .then(() => {
              if (cfg.debug) {
                log.info?.(
                  `[memcraft] before_prompt_build async_store ok captured=${pendingTurnMessages.length} session=${sessionKey || "-"}`,
                );
              }
            })
            .catch((err) => {
              log.warn?.(`[memcraft] before_prompt_build async_store failed: ${String(err)}`);
            });
        } else if (cfg.debug) {
            log.info?.(`[memcraft] before_prompt_build async_store skip no_previous_real_turn session=${sessionKey || "-"}`);
        }
      }

      const query = extractQuery(event, cfg);
      if (!query) return;
      if (isInternalProtocolQuery(query)) {
        if (cfg.debug) {
          log.info?.("[memcraft] before_prompt_build skip internal_protocol_query");
        }
        return;
      }
      if (cfg.debug) {
        log.info?.(`[memcraft] before_prompt_build query_len=${query.length} session=${sessionKey || "-"}`);
        const rawPrompt = String(event?.prompt || "");
        const promptPreview = (cfg.stripHistoryMemory ? stripMemoryContext(rawPrompt) : rawPrompt).slice(0, 300);
        log.info?.(`[memcraft] before_prompt_build query_preview=${query.slice(0, 300)}`);
        log.info?.(`[memcraft] before_prompt_build event_prompt_preview=${promptPreview}`);
      }

      try {
        const retrieveMessages = buildRetrieveMessages(event, cfg, query);
        const canonicalSessionId = resolveCanonicalSessionId(ctx);
        const retrievePayload = {
          query,
          top_k: cfg.topK,
          session_id: cfg.retrieveBySession ? canonicalSessionId : null,
          agent_id: ctx?.agentId || null,
          namespace: cfg.namespace,
          messages: retrieveMessages,
        };
        if (cfg.debug) {
          retrievePayload.debug_render_prompt = true;
          retrievePayload.prompt_title = cfg.promptTitle;
          retrievePayload.base_prompt = query;
          log.info?.(`[memcraft] retrieve wrapped_query=${JSON.stringify(retrievePayload)}`);
        }

        const result = await retrieveMemory(cfg, retrievePayload);

        const block = buildMemoryBlock(cfg.promptTitle, result?.memory_context || "");
        if (!block) return;
        const rawBasePrompt = String(event?.prompt || query || "");
        const basePrompt = cfg.stripHistoryMemory ? stripMemoryContext(rawBasePrompt) : rawBasePrompt;
        const finalPromptPreview = `${block}${basePrompt ? `\n${basePrompt}` : ""}`.slice(
          0,
          cfg.contextPreviewMaxChars,
        );
        if (cfg.debug) {
          log.info?.(`[memcraft] retrieve ok items=${result?.items?.length || 0}`);
          log.info?.(`[memcraft] memory_block=${block.slice(0, 1200)}`);
          if (result?.debug?.final_prompt_preview) {
            log.info?.(`[memcraft] wrapped_prompt_preview=${result.debug.final_prompt_preview}`);
          }
        }

        if (cfg.logFinalContext) {
          log.info?.(`[memcraft] final_prompt_preview=${finalPromptPreview}`);
        }

        // Return multiple compatible fields for different OpenClaw plugin API versions.
        return {
          prependContext: block,
          prependPrompt: block,
          memoryContext: block,
        };
      } catch (err) {
        log.warn?.(`[memcraft] retrieve failed: ${String(err)}`);
      }
    });

    api.on("subagent_spawned", async (event) => {
      const childSessionKey = String(event?.childSessionKey || "").trim();
      if (!childSessionKey) return;
      subagentSessionKeys.add(childSessionKey);
      if (cfg.debug) {
        log.info?.(`[memcraft] subagent_spawned child_session=${childSessionKey}`);
      }
    });

    api.on("subagent_ended", async (event) => {
      const targetSessionKey = String(event?.targetSessionKey || "").trim();
      if (!targetSessionKey) return;
      subagentSessionKeys.delete(targetSessionKey);
      if (cfg.debug) {
        log.info?.(`[memcraft] subagent_ended target_session=${targetSessionKey}`);
      }
    });

    api.on("agent_end", async (event, ctx) => {
      if (!event?.success || !Array.isArray(event?.messages)) return;
      if (isExcludedAgent(cfg, ctx)) return;

      const sessionKey = resolveSessionKey(ctx);
      if (!sessionKey) return;

      if (isSubagentSession(ctx)) {
        if (cfg.debug) {
          log.info?.(`[memcraft] agent_end skip subagent_session session=${sessionKey}`);
        }
        return;
      }

      try {
        if (cfg.storeGranularity === "agent_end") {
          const lastTurnMessages = pickLastCompletedTurnFromAgentEnd(event.messages, cfg);
          if (!lastTurnMessages.length) {
            if (cfg.debug) {
              log.info?.(`[memcraft] agent_end cache skip no_completed_real_turn session=${sessionKey}`);
            }
            return;
          }
          pendingAgentEndStoreBySession.set(sessionKey, lastTurnMessages);
          if (cfg.debug) {
            log.info?.(`[memcraft] agent_end cached turn messages=${lastTurnMessages.length} session=${sessionKey}`);
          }
          return;
        }

        const messages = pickMessages(event.messages, cfg);
        if (!messages.length) return;

        if (cfg.storeGranularity !== "session_end") return;

        // session_end payload does not include full messages, so cache the latest
        // captured snapshot and flush it only when session_end fires.
        pendingStoreBySession.set(sessionKey, messages);
        if (cfg.debug) {
          log.info?.(`[memcraft] agent_end buffered messages=${messages.length} session=${sessionKey}`);
        }
      } catch (err) {
        log.warn?.(`[memcraft] agent_end buffer failed: ${String(err)}`);
      }
    });

    api.on("session_end", async (event, ctx) => {
      if (isExcludedAgent(cfg, ctx)) {
        if (cfg.debug) {
          log.info?.(`[memcraft] session_end skip excluded_agent=${ctx?.agentId || "-"}`);
        }
        return;
      }

      const sessionKey = resolveSessionKey(ctx);

      if (cfg.storeGranularity !== "session_end") {
        if (cfg.storeGranularity === "agent_end") {
          const tailTurnMessages = pendingAgentEndStoreBySession.get(sessionKey) || [];
          if (!tailTurnMessages.length) {
            if (cfg.debug) {
              log.info?.(`[memcraft] session_end no_pending_agent_end_turn session=${sessionKey || "-"}`);
            }
            return;
          }

          try {
            pendingAgentEndStoreBySession.delete(sessionKey);
            const canonicalSessionId = resolveCanonicalSessionId(ctx);
            await storeConversation(cfg, {
              session_id: canonicalSessionId,
              agent_id: ctx?.agentId || null,
              namespace: cfg.namespace,
              trigger: "session_end_tail_flush_for_agent_end",
              messages: tailTurnMessages,
            });
            if (cfg.debug) {
              log.info?.(`[memcraft] session_end tail_flush ok captured=${tailTurnMessages.length} session=${sessionKey || "-"}`);
            }
          } catch (err) {
            log.warn?.(`[memcraft] session_end tail_flush failed: ${String(err)}`);
          }
          return;
        }

        if (cfg.debug) {
          log.info?.(`[memcraft] session_end skip store_granularity=${cfg.storeGranularity}`);
        }
        return;
      }

      const sourceMessages = pendingStoreBySession.get(sessionKey) || [];
      if (!sourceMessages.length) {
        if (cfg.debug) {
          log.info?.(
            `[memcraft] session_end no_buffered_messages session=${sessionKey || "-"} message_count=${event?.messageCount ?? "-"}`,
          );
        }
        return;
      }

      if (cfg.debug) {
        log.info?.(
          `[memcraft] session_end messages=${sourceMessages.length} session=${sessionKey || "-"}`,
        );
      }

      try {
        pendingStoreBySession.delete(sessionKey);
        const canonicalSessionId = resolveCanonicalSessionId(ctx);

        await storeConversation(cfg, {
          session_id: canonicalSessionId,
          agent_id: ctx?.agentId || null,
          namespace: cfg.namespace,
          trigger: "session_end",
          messages: sourceMessages,
        });
        if (cfg.debug) {
          log.info?.(`[memcraft] session_end store ok captured=${sourceMessages.length}`);
        }
      } catch (err) {
        log.warn?.(`[memcraft] session_end store failed: ${String(err)}`);
      }
    });
  },
};
