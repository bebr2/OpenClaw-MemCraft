from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


class BaseMemory(ABC):
    """Abstract memory store with retrieval, compression and persistence APIs."""

    @abstractmethod
    def retrieve(
        self,
        conversation: dict[str, Any],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve relevant memory items and return memory context payload."""

    # @abstractmethod
    # def compress(self, conversation: dict[str, Any], prompt: str | None = None) -> dict[str, Any]:
    #     """Compress raw conversation into a memory item representation."""

    @abstractmethod
    def store(self, conversation: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store compressed memory item and return storage result."""

    @abstractmethod
    def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
        """List persisted memory items for dashboard or debugging."""
        
    @abstractmethod
    def _load_memories(self) -> None:
        """Load persisted memories from storage into memory; called at init."""

    @abstractmethod
    def _save_memories(self, **kwargs: Any) -> None:
        """Persist memories from memory into storage; called after store()."""
        
    def stats(self) -> dict[str, Any]:
        """Return store stats for dashboard."""
        rows = self._get_documents()
        namespaces: dict[str, int] = {}
        for row in rows:
            ns = row.get("namespace") or "default"
            namespaces[ns] = namespaces.get(ns, 0) + 1

        dashboard_tz, dashboard_tz_name = self._resolve_dashboard_timezone()
        today = datetime.now(dashboard_tz).date()
        seven_days: list[dict[str, Any]] = []
        count_by_day: dict[str, int] = {}
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            key = day.isoformat()
            count_by_day[key] = 0
            seven_days.append({"date": key, "count": 0})

        for row in rows:
            created_at = str(row.get("created_at") or "").strip()
            if not created_at:
                continue
            normalized = created_at.replace("Z", "+00:00")
            try:
                created_dt = datetime.fromisoformat(normalized)
            except ValueError:
                continue
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            day_key = created_dt.astimezone(dashboard_tz).date().isoformat()
            if day_key in count_by_day:
                count_by_day[day_key] += 1

        for row in seven_days:
            row["count"] = count_by_day.get(row["date"], 0)

        term_df = getattr(self, "term_df", {})
        avgdl = float(getattr(self, "avgdl", 0.0) or 0.0)
        if avgdl <= 0.0:
            avgdl = self._fallback_avg_document_length(rows)
        return {
            "total_documents": len(rows),
            "avg_document_length": avgdl,
            "recent_7d_total": sum(item["count"] for item in seven_days),
            "recent_7d_daily": seven_days,
            "dashboard_timezone": dashboard_tz_name,
            "unique_terms": len(term_df) if isinstance(term_df, dict) else 0,
            "namespaces": namespaces,
        }

    def _resolve_dashboard_timezone(self) -> tuple[Any, str]:
        """
        Resolve dashboard timezone for date-bucket statistics.

        MEMORY_DASHBOARD_TIMEZONE supports:
        - local (default): server local timezone
        - IANA timezone name, e.g. Asia/Shanghai, America/Los_Angeles
        """
        raw = str(os.getenv("MEMORY_DASHBOARD_TIMEZONE") or "local").strip()
        if not raw or raw.lower() == "local":
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            local_name = getattr(local_tz, "key", None) or str(local_tz) or "local"
            return local_tz, local_name

        if ZoneInfo is not None:
            try:
                tz = ZoneInfo(raw)
                return tz, raw
            except Exception:
                pass

        return timezone.utc, "UTC"

    def _fallback_avg_document_length(self, rows: list[dict[str, Any]]) -> float:
        """Estimate average doc length for stores that do not maintain BM25-style avgdl."""
        if not rows:
            return 0.0

        total_len = 0
        valid_rows = 0
        for row in rows:
            length = self._estimate_row_length(row)
            if length <= 0:
                continue
            total_len += length
            valid_rows += 1

        if valid_rows <= 0:
            return 0.0
        return total_len / valid_rows

    def _estimate_row_length(self, row: dict[str, Any]) -> int:
        if not isinstance(row, dict):
            return 0

        summary = str(row.get("summary") or row.get("title") or "")
        body = str(
            row.get("compressed_memory")
            or row.get("content")
            or row.get("text")
            or row.get("detail")
            or ""
        )
        tags = row.get("tags")
        tags_text = " ".join(str(t) for t in tags) if isinstance(tags, list) else ""

        text = " ".join([summary, body, tags_text]).strip().lower()
        if not text:
            return 0

        latin_tokens = re.findall(r"[a-z0-9_]+", text)
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
        return len(latin_tokens) + len(cjk_chars)

    def _get_documents(self) -> list[dict[str, Any]]:
        """Return in-memory document rows; stores can override if needed."""
        rows = getattr(self, "documents", [])
        return rows if isinstance(rows, list) else []

    def build_list_memories_payload(
        self,
        limit: int = 200,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Build a frontend-safe memories payload while preserving backend-defined fields."""
        raw_rows = self.list_memories(limit=limit, namespace=namespace)
        normalized_rows = [self._normalize_memory_row(row) for row in raw_rows]
        return {
            "schema_version": 1,
            "store_type": self.__class__.__name__,
            "items": normalized_rows,
            "raw_items": raw_rows,
        }

    def _normalize_memory_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize a memory row into a stable display contract.

        Different stores can return different schemas in list_memories; this method gives
        the frontend a predictable shape while preserving all custom fields in raw.
        """
        if not isinstance(row, dict):
            row = {"value": row}

        summary = self._pick_first_non_empty(row, ["summary", "title", "name", "context", "category"])
        body = self._pick_first_non_empty(
            row,
            ["compressed_memory", "content", "text", "detail", "details", "memory_context"],
        )

        normalized = {
            "id": row.get("id"),
            "created_at": row.get("created_at") or row.get("timestamp") or row.get("time"),
            "session_id": row.get("session_id") or row.get("session") or row.get("conversation_id"),
            "namespace": row.get("namespace") or row.get("space") or "default",
            "source": row.get("compression_source") or row.get("source") or row.get("store_type") or "-",
            "summary": summary,
            "compressed_memory": body,
            "display": {
                "title": summary,
                "body": body,
                "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
                "meta": {
                    "category": row.get("category"),
                    "context": row.get("context"),
                    "score": row.get("score"),
                },
            },
            "raw": row,
        }
        return normalized

    def _pick_first_non_empty(self, row: dict[str, Any], keys: list[str]) -> str:
        for key in keys:
            val = row.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
        return ""

    def _looks_like_internal_protocol_text(self, text: str) -> bool:
        s = (text or "").strip().lower()
        if not s:
            return False

        markers = [
            "please output only valid json object",
            "return strict json only",
            "json schema to follow",
            "memory compression protocol",
            "group chat memory compression",
            "符合json schema",
            "严格json",
            "严格的json对象",
            "valid json object",
        ]
        return any(marker in s for marker in markers)

    def _is_internal_protocol_noise(self, messages: list[dict[str, Any]]) -> bool:
        if not messages:
            return False
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        return self._looks_like_internal_protocol_text(joined)

    def _is_recent_duplicate(
        self,
        namespace: str,
        summary: str,
        compressed_memory: str,
        tags: list[str],
        lookback: int = 80,
    ) -> bool:
        key = "\n".join(
            [
                (summary or "").strip().lower(),
                (compressed_memory or "").strip().lower(),
                "|".join(sorted((t or "").strip().lower() for t in (tags or []) if (t or "").strip())),
            ]
        )
        if not key.strip():
            return False

        docs = self._get_documents()
        for row in reversed(docs[-max(1, int(lookback)):]):
            if (row.get("namespace") or "default") != namespace:
                continue

            old_key = "\n".join(
                [
                    str(row.get("summary") or "").strip().lower(),
                    str(row.get("compressed_memory") or "").strip().lower(),
                    "|".join(
                        sorted(
                            str(t or "").strip().lower()
                            for t in (row.get("tags") or [])
                            if str(t or "").strip()
                        )
                    ),
                ]
            )
            if old_key == key:
                return True
        return False

    def _sanitize_user_preview_for_log(self, text: str, max_chars: int = 180) -> str:
        """Build a readable preview for logs by removing known wrapper noise."""
        preview = (text or "").strip()
        if not preview:
            return "Conversation memory"

        # Strip sender metadata blocks wrapped as fenced JSON.
        preview = re.sub(
            r"Sender \(untrusted metadata\):\s*```json[\s\S]*?```\s*",
            "",
            preview,
            flags=re.IGNORECASE,
        ).strip()

        # Strip leading bracket timestamps like: [Tue 2026-03-10 21:08 GMT+8]
        preview = re.sub(r"^\[[^\]]+\]\s*", "", preview).strip()

        if not preview:
            preview = "Conversation memory"
        return preview[:max_chars]

    def _default_build_memory_context(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return ""

        lines: list[str] = []
        for idx, item in enumerate(items, start=1):
            line = (
                f"[{idx}] score={item.get('score')} "
                f"summary={item.get('summary', '').strip()} "
            )
            lines.append(line.strip())

        context = "\n".join(lines).strip()
        context_max_chars = max(500, int(getattr(self, "context_max_chars", 4000)))
        if len(context) > context_max_chars:
            return context[:context_max_chars] + "..."
        return context

    def render_memory_block(self, title: str, memory_context: str) -> str:
        body = (memory_context or "").strip()
        if not body:
            return ""
        return "\n".join(
            [
                f"<memories title=\"{title}\">",
                body,
                "</memories>",
            ]
        )
        
    def _default_extract_retrieve_query(self, conversation: dict[str, Any]) -> str:
        messages = conversation.get("messages") if isinstance(conversation, dict) else None
        if isinstance(messages, list):
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i] if isinstance(messages[i], dict) else {}
                if msg.get("role") != "user":
                    continue
                text = self._default_extract_text(msg.get("content"))
                text = self._default_sanitize_retrieve_text(text)
                if text:
                    return text

        prompt = ""
        if isinstance(conversation, dict):
            prompt = str(conversation.get("prompt") or conversation.get("query") or "")
        return self._default_sanitize_retrieve_text(prompt)

    def _default_extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                    continue
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                    continue
                if isinstance(part.get("content"), str):
                    parts.append(part["content"])
            return "\n".join(parts).strip()
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            if isinstance(content.get("content"), str):
                return content["content"]
        return ""

    def _default_sanitize_retrieve_text(self, text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"<memory_context\b[^>]*>[\s\S]*?</memory_context>\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<memories\b[^>]*>[\s\S]*?</memories>\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"^\s*\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[+-]\d{1,2}(?::?\d{2})?\]\s*",
            "",
            cleaned,
        )
        return cleaned.strip()

