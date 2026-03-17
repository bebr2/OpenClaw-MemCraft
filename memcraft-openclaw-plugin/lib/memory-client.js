async function requestJson(url, payload, timeoutMs, label = "request") {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${body}`);
    }

    return await res.json();
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(`${label} timeout after ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function retrieveMemory(cfg, payload) {
  return requestJson(`${cfg.serverUrl}/retrieve`, payload, cfg.retrieveTimeoutMs, "retrieve");
}

export async function storeConversation(cfg, payload) {
  return requestJson(`${cfg.serverUrl}/store`, payload, cfg.storeTimeoutMs, "store");
}
