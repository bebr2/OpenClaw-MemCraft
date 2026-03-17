from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib import error, request
from typing import Any


logger = logging.getLogger(__name__)


class LLMTaskClient:
    """Wrapper for OpenClaw Gateway OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        gateway_base_url: str = "http://127.0.0.1:18789",
        gateway_token: str | None = None,
        agent_id: str = "main",
        model: str = "openclaw",
        timeout_ms: int = 30000,
    ) -> None:
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.gateway_token = gateway_token or self._read_gateway_token_from_openclaw_config()
        self.agent_id = agent_id
        self.model = model
        self.timeout_ms = timeout_ms

    def get_llm_response(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if type(messages) == str:
            messages = [{"role": "user", "content": messages}]
        if not self.gateway_token:
            raise RuntimeError(
                "Gateway token is required. Set MEMORY_LLM_GATEWAY_TOKEN/OPENCLAW_GATEWAY_TOKEN "
                "or configure ~/.openclaw/openclaw.json"
            )

        outgoing_messages = list(messages)
        if schema:
            outgoing_messages = [
                {
                    "role": "system",
                    "content": (
                        "When possible, format the output as JSON matching this schema:\n"
                        f"{json.dumps(schema, ensure_ascii=False)}"
                    ),
                },
                *outgoing_messages,
            ]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": outgoing_messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url=f"{self.gateway_base_url}/v1/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.gateway_token}",
                "Content-Type": "application/json",
                "x-openclaw-agent-id": self.agent_id,
            },
        )

        try:
            with request.urlopen(req, timeout=max(5, int(self.timeout_ms / 1000) + 5)) as resp:
                response_text = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise RuntimeError(f"Gateway chat completions failed: HTTP {exc.code}, {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Gateway chat completions network error: {exc}") from exc

        return self._extract_response_text(response_text)

    def _extract_response_text(self, output: str) -> str:
        """Parse Gateway response and return model message text when possible."""
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return output.strip()

        if not isinstance(parsed, dict):
            return output.strip()

        choices = parsed.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()

        # Keep compatibility with wrappers that return payload under details/json keys.
        payload = self._extract_payload(parsed)
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False)

        return output.strip()

    def _extract_json(self, output: str) -> dict[str, Any]:
        """Parse Gateway response and return business JSON payload."""
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                payload = self._extract_payload(parsed)
                if payload is not None:
                    return payload
        except json.JSONDecodeError:
            pass

        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = self._extract_payload(parsed)
                if payload is not None:
                    return payload

        raise RuntimeError(f"Cannot parse LLM response as JSON: {output[:500]}")

    def _extract_payload(self, parsed: dict[str, Any]) -> dict[str, Any] | None:
        """Extract the business JSON from OpenClaw/OpenAI response shapes."""
        # OpenAI-compatible shape: {"choices": [{"message": {"content": "{...}"}}]}
        choices = parsed.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return self._extract_json_from_text(content)

        # Legacy shape: { ..., "details": { "json": { ... } } }
        details = parsed.get("details")
        if isinstance(details, dict):
            details_json = details.get("json")
            if isinstance(details_json, dict):
                return details_json
            if isinstance(details_json, str):
                try:
                    obj = json.loads(details_json)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    logger.debug("gateway details.json was not valid JSON string")

        # Fallback shape in some wrappers: { "json": { ... } }
        top_json = parsed.get("json")
        if isinstance(top_json, dict):
            return top_json
        if isinstance(top_json, str):
            try:
                obj = json.loads(top_json)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                logger.debug("gateway top-level json was not valid JSON string")

        # If the parsed object itself already looks like target data, accept it.
        if all(k in parsed for k in ("summary", "compressed_memory", "tags")):
            return parsed

        # Otherwise return raw object as a last resort so callers can still inspect keys.
        return parsed

    def _extract_json_from_text(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise RuntimeError("LLM returned empty message content")

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Recover JSON object if the model included extra narration.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            snippet = text[start : end + 1]
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        raise RuntimeError(f"Model content is not valid JSON object: {text[:500]}")

    def _read_gateway_token_from_openclaw_config(self) -> str | None:
        env_token = os.getenv("MEMORY_LLM_GATEWAY_TOKEN") or os.getenv("OPENCLAW_GATEWAY_TOKEN")
        if env_token:
            return env_token

        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if not cfg_path.exists():
            return None

        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        gateway = cfg.get("gateway") if isinstance(cfg, dict) else None
        auth = gateway.get("auth") if isinstance(gateway, dict) else None
        token = auth.get("token") if isinstance(auth, dict) else None
        return str(token).strip() if token else None
