from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base_memory import BaseMemory
from .llm_task_client import LLMTaskClient


logger = logging.getLogger(__name__)


class BM25Memory(BaseMemory):
    """Simple local BM25 memory store backed by JSONL files."""

    def __init__(
        self,
        data_dir: str,
        llm_client: LLMTaskClient | None = None,
        default_top_k: int = 5,
        context_max_chars: int = 4000,
        llm_prompt_template: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.data_dir / "memory_items.jsonl"

        self.llm_client = llm_client
        self.default_top_k = max(1, int(default_top_k))
        self.context_max_chars = max(500, int(context_max_chars))
        self.llm_prompt_template = (
            llm_prompt_template
            or "You are a memory compressor. Convert the conversation into short, retrievable memory. Output must match schema."
        )

        self.documents: list[dict[str, Any]] = []
        self.doc_tokens: list[list[str]] = []
        self.doc_freqs: list[dict[str, int]] = []
        self.term_df: dict[str, int] = {}
        self.avgdl = 0.0
        self.k1 = 1.5
        self.b = 0.75

        self._load_memories()
        self._rebuild_index()

    def retrieve(
        self,
        conversation: dict[str, Any],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        q = self._default_extract_retrieve_query(conversation)
        if not q:
            return {"query": "", "memory_context": ""}

        namespace = (filters or {}).get("namespace")
        session_id = (filters or {}).get("session_id")
        top_k = max(1, int(top_k or self.default_top_k))
        print("Query:", q)
        q_tokens = self._tokenize(q)
        if not q_tokens:
            return {"query": q, "items": [], "memory_context": ""}

        scored: list[tuple[float, int]] = []
        for idx, doc in enumerate(self.documents):
            if namespace and doc.get("namespace") != namespace:
                continue
            if session_id and doc.get("session_id") not in {session_id, None, ""}:
                continue

            score = self._bm25_score(q_tokens, idx)
            if score > 0:
                scored.append((score, idx))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        picked = scored[:top_k]

        items: list[dict[str, Any]] = []
        for score, idx in picked:
            doc = self.documents[idx]
            items.append(
                {
                    "id": doc.get("id"),
                    "score": round(score, 6),
                    "summary": doc.get("summary", ""),
                    "compressed_memory": doc.get("compressed_memory", ""),
                    "created_at": doc.get("created_at"),
                    "session_id": doc.get("session_id"),
                    "namespace": doc.get("namespace"),
                    "tags": doc.get("tags", []),
                }
            )

        return {
            "query": q,
            "memory_context": self._default_build_memory_context(items),
        }
        
    def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
        """List persisted memory items for dashboard or debugging."""
        rows = self._get_documents()
        if namespace:
            rows = [row for row in rows if row.get("namespace") == namespace]
        rows = rows[-max(1, int(limit)):]
        return list(reversed(rows))

    
    def _compress(self, conversation: dict[str, Any], prompt: str | None = None) -> dict[str, Any]:
        messages = conversation.get("messages") or []

        user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
        assistant_texts = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
        user_last = user_texts[-1].strip() if user_texts else ""
        assistant_last = assistant_texts[-1].strip() if assistant_texts else ""
        fallback_summary_preview = self._sanitize_user_preview_for_log(user_last)

        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "compressed_memory": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["summary", "compressed_memory", "tags"],
            "additionalProperties": False,
        }

        if self.llm_client is not None:
            try:
                logger.info(
                    "[memory.compress] gateway llm invoke BEFORE summary=%s",
                    fallback_summary_preview,
                )
                llm_result = self._invoke_json(
                    prompt=prompt or self.llm_prompt_template,
                    input_data={"messages": messages},
                    schema=schema,
                )
                llm_summary = str(llm_result.get("summary") or "").strip()
                logger.info(
                    "[memory.compress] gateway llm invoke AFTER summary=%s",
                    llm_summary,
                )
                return {
                    "summary": llm_summary,
                    "compressed_memory": (llm_result.get("compressed_memory") or "").strip(),
                    "tags": [str(x).strip() for x in (llm_result.get("tags") or []) if str(x).strip()],
                    "compression_source": "gateway-openai-http",
                }
            except Exception as exc:
                logger.warning("[memory.compress] gateway llm invoke failed, using fallback: %s", exc)
                pass

        # Fallback compression keeps the implementation resilient when llm-task is unavailable.
        summary = fallback_summary_preview
        compressed_memory = "\n".join(
            [
                f"User: {user_last[:400]}" if user_last else "",
                f"Assistant: {assistant_last[:400]}" if assistant_last else "",
            ]
        ).strip()

        if not compressed_memory:
            compressed_memory = " ".join((m.get("content", "") for m in messages[:4])).strip()[:800]

        return {
            "summary": summary,
            "compressed_memory": compressed_memory,
            "tags": [],
            "compression_source": "fallback",
        }

    def store(self, conversation: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        messages = conversation.get("messages") or []
        if self._is_internal_protocol_noise(messages):
            logger.info("[memory.store] skipped internal protocol/noise payload")
            return {
                "stored": False,
                "skipped": True,
                "reason": "internal_protocol_noise",
            }
        compressed = self._compress(conversation, prompt=(metadata or {}).get("compress_prompt"))
        namespace = conversation.get("namespace") or "default"

        if self._is_recent_duplicate(
            namespace=namespace,
            summary=str(compressed.get("summary") or ""),
            compressed_memory=str(compressed.get("compressed_memory") or ""),
            tags=[str(x) for x in (compressed.get("tags") or [])],
        ):
            logger.info("[memory.store] skipped duplicate compressed memory namespace=%s", namespace)
            return {
                "stored": False,
                "skipped": True,
                "reason": "duplicate_memory",
            }

        item = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": conversation.get("session_id"),
            "agent_id": conversation.get("agent_id"),
            "namespace": namespace,
            "summary": compressed.get("summary", ""),
            "compressed_memory": compressed.get("compressed_memory", ""),
            "tags": compressed.get("tags", []),
            "compression_source": compressed.get("compression_source", "unknown"),
            "messages": conversation.get("messages") or [],
            "meta": metadata or {},
        }

        self._save_memories(item=item)
        self.documents.append(item)
        self._append_to_index(item)

        return {
            "stored": True,
            "item_id": item["id"],
            "compression_source": item["compression_source"],
        }

    def _invoke_json(
        self,
        prompt: str,
        input_data: dict[str, Any],
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if self.llm_client is None:
            raise RuntimeError("LLM client is unavailable")

        system_prompt = (
            f"{prompt}\n\n"
            "Return strict JSON only. Do not include markdown code fences.\n"
            f"Input JSON:\n{json.dumps(input_data, ensure_ascii=False)}"
        )
        if schema:
            system_prompt += f"\nJSON Schema to follow:\n{json.dumps(schema, ensure_ascii=False)}"

        response_text = self.llm_client.get_llm_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Please output only valid JSON object."},
            ],
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return self._extract_json_from_text(response_text)

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

    def _load_memories(self) -> None:
        if not self.memory_file.exists():
            return

        docs: list[dict[str, Any]] = []
        with self.memory_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self.documents = docs

    def _save_memories(self, **kwargs: Any) -> None:
        item = kwargs["item"]
        with self.memory_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _tokenize(self, text: str) -> list[str]:
        lowered = (text or "").lower().strip()
        if not lowered:
            return []

        latin_words = re.findall(r"[a-z0-9_]+", lowered)
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
        return latin_words + cjk_chars

    def _rebuild_index(self) -> None:
        self.doc_tokens = []
        self.doc_freqs = []
        self.term_df = {}

        total_len = 0
        for doc in self.documents:
            content = " ".join(
                [
                    str(doc.get("summary") or ""),
                    str(doc.get("compressed_memory") or ""),
                    " ".join(str(t) for t in doc.get("tags") or []),
                ]
            ).strip()
            tokens = self._tokenize(content)
            self.doc_tokens.append(tokens)
            total_len += len(tokens)

            freqs: dict[str, int] = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            self.doc_freqs.append(freqs)

            for token in freqs.keys():
                self.term_df[token] = self.term_df.get(token, 0) + 1

        doc_count = len(self.documents)
        self.avgdl = (total_len / doc_count) if doc_count else 0.0

    def _append_to_index(self, doc: dict[str, Any]) -> None:
        content = " ".join(
            [
                str(doc.get("summary") or ""),
                str(doc.get("compressed_memory") or ""),
                " ".join(str(t) for t in doc.get("tags") or []),
            ]
        ).strip()
        tokens = self._tokenize(content)
        self.doc_tokens.append(tokens)

        freqs: dict[str, int] = {}
        for token in tokens:
            freqs[token] = freqs.get(token, 0) + 1
        self.doc_freqs.append(freqs)

        for token in freqs.keys():
            self.term_df[token] = self.term_df.get(token, 0) + 1

        doc_count = len(self.documents)
        if doc_count == 0:
            self.avgdl = 0.0
        else:
            total_len = sum(len(tokens_row) for tokens_row in self.doc_tokens)
            self.avgdl = total_len / doc_count

    def _bm25_score(self, query_tokens: list[str], doc_idx: int) -> float:
        if doc_idx >= len(self.doc_freqs):
            return 0.0
        if not query_tokens:
            return 0.0

        freqs = self.doc_freqs[doc_idx]
        dl = len(self.doc_tokens[doc_idx]) or 1
        n_docs = len(self.documents)

        score = 0.0
        for token in query_tokens:
            f = freqs.get(token, 0)
            if f <= 0:
                continue

            df = self.term_df.get(token, 0)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5)) if df > 0 else 0.0

            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
            score += idf * (f * (self.k1 + 1) / denom)

        return score

