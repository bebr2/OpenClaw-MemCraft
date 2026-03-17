from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base_memory import BaseMemoryStore
from .llm_task_client import LLMTaskClient

# Use vendored MemoryOS source under memory/MemoryOS when package is not installed.
_MEMORYOS_VENDOR_ROOT = Path(__file__).resolve().parent / "MemoryOS"
if str(_MEMORYOS_VENDOR_ROOT) not in sys.path:
	sys.path.insert(0, str(_MEMORYOS_VENDOR_ROOT))

from .MemoryOS.memoryos_chromadb import Memoryos


logger = logging.getLogger(__name__)


class _MemoryOSLLMTaskAdapter:
	"""Adapter to satisfy MemoryOS OpenAIClient-like interface via LLMTaskClient."""

	def __init__(self, llm_client: LLMTaskClient, default_model: str = "openclaw") -> None:
		self._llm_client = llm_client
		self._default_model = default_model

	def chat_completion(self, model, messages, temperature=0.1, max_tokens=2048):
		# `model` is ignored here because LLMTaskClient already owns selected model config.
		_ = model or self._default_model
		return self._llm_client.get_llm_response(
			messages=messages,
			temperature=temperature,
			max_tokens=max_tokens,
		)


class MemoryOSMemoryStore(BaseMemoryStore):
	"""MemoryOS-backed memory store with local persistence."""

	def __init__(
		self,
		data_dir: str,
		llm_client: LLMTaskClient | None = None,
		retrieve_model: str = "all-MiniLM-L6-v2",
		default_top_k: int = 5,
		context_max_chars: int = 4000,
	) -> None:
		self.data_dir = Path(data_dir)
		self.data_dir.mkdir(parents=True, exist_ok=True)

		self.memoryos_dir = self.data_dir / "memoryos"
		self.memoryos_dir.mkdir(parents=True, exist_ok=True)

		self.default_top_k = max(1, int(default_top_k))
		self.context_max_chars = max(500, int(context_max_chars))

		# Keep constructor compatibility with app bootstrap.
		self.llm_client = llm_client

		self.user_id = os.getenv("MEMORY_MEMORYOS_USER_ID", "default-user")
		self.assistant_id = os.getenv("MEMORY_MEMORYOS_ASSISTANT_ID", "default-assistant")
		self._build_memory_system(retrieve_model=retrieve_model)

		self.documents: list[dict[str, Any]] = []
		self._load_memories()
		self.documents = self.list_memories(limit=100000)

	def _build_memory_system(self, retrieve_model: str) -> None:
		llm_model = os.getenv("MEMORY_MEMORYOS_LLM_MODEL") or os.getenv("MEMORY_LLM_MODEL") or "openclaw"
		self.memory_system = Memoryos(
			user_id=self.user_id,
			openai_api_key=os.getenv("OPENAI_API_KEY", "dummy"),
			openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
			data_storage_path=str(self.memoryos_dir),
			llm_model=llm_model,
			assistant_id=self.assistant_id,
			short_term_capacity=int(os.getenv("MEMORY_MEMORYOS_SHORT_TERM_CAPACITY", "7")),
			retrieval_queue_capacity=max(1, int(self.default_top_k)),
			long_term_knowledge_capacity=int(os.getenv("MEMORY_MEMORYOS_LONG_TERM_CAPACITY", "100")),
			mid_term_heat_threshold=float(os.getenv("MEMORY_MEMORYOS_MID_TERM_HEAT_THRESHOLD", "5")),
			embedding_model_name=retrieve_model,
		)

		# Align with other baselines: route all MemoryOS LLM calls through shared LLMTaskClient.
		if self.llm_client is not None:
			adapter = _MemoryOSLLMTaskAdapter(self.llm_client, default_model=llm_model)
			self.memory_system.client = adapter
			self.memory_system.mid_term_memory.client = adapter
			self.memory_system.updater.client = adapter
			self.memory_system.user_long_term_memory.llm_interface = adapter
			self.memory_system.assistant_long_term_memory.llm_interface = adapter

	def retrieve(
		self,
		conversation: dict[str, Any],
		top_k: int = 5,
		filters: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		query = self._default_extract_retrieve_query(conversation)
		if not query:
			return {"query": "", "memory_context": ""}

		top_k = max(1, int(top_k or self.default_top_k))
		user_prompt = self.memory_system.get_user_prompt(query, topk=top_k)

		return {
			"query": query,
			"memory_context": user_prompt.split(f"The user just said: {query}")[0].strip(),
		}

	def store(self, conversation: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
		messages = conversation.get("messages") or []
		namespace = conversation.get("namespace") or "default"
		session_id = conversation.get("session_id")

		if not isinstance(messages, list) or not messages:
			return {
				"stored": False,
				"skipped": True,
				"reason": "empty_messages",
			}

		if self._is_internal_protocol_noise(messages):
			logger.info("[memory.store] skipped internal protocol/noise payload")
			return {
				"stored": False,
				"skipped": True,
				"reason": "internal_protocol_noise",
			}

		pairs = self._extract_user_assistant_pairs(messages)
		if not pairs:
			return {
				"stored": False,
				"skipped": True,
				"reason": "no_valid_user_assistant_pairs",
			}

		stored_count = 0
		for user_input, agent_response in pairs:
			if not user_input and not agent_response:
				continue
			self.memory_system.add_memory(
				user_input=user_input,
				agent_response=agent_response,
				meta_data={
					"namespace": namespace,
					"session_id": session_id,
					**(metadata or {}),
				},
			)
			stored_count += 1

		self._save_memories()
		self.documents = self.list_memories(limit=100000)

		return {
			"stored": stored_count > 0,
			"stored_count": stored_count,
			"namespace": namespace,
			"session_id": session_id,
			"id": str(uuid.uuid4()),
		}

	def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
		ns = namespace or "default"
		row_limit = max(1, int(limit))

		rows: list[dict[str, Any]] = []
		now_iso = datetime.now(timezone.utc).isoformat()
		storage = self.memory_system.storage_provider

		short_term_memories = storage.metadata.get("short_term_memory", []) or []
		for idx, qa in enumerate(short_term_memories):
			user_input = self._clean_message_text(qa.get("user_input"))
			agent_response = self._clean_message_text(qa.get("agent_response"))
			text = f"User: {user_input}\nAssistant: {agent_response}".strip()
			if not text:
				continue

			ts = self._normalize_timestamp(qa.get("timestamp")) or now_iso
			rows.append(
				{
					"id": qa.get("id") or f"short-term-{idx}",
					"created_at": ts,
					"namespace": ns,
					"session_id": None,
					"summary": user_input[:200] if user_input else "Short-term memory",
					"content": text,
					"compressed_memory": text,
					"category": "short_term",
					"tags": [],
					"keywords": [],
					"retrieval_count": 0,
					"last_accessed": ts,
					"source": "memoryos",
				}
			)

		mid_term_sessions = storage.metadata.get("mid_term_sessions", {}) or {}
		for session_id, session in mid_term_sessions.items():
			pages = session.get("pages_backup") or []
			if not pages:
				summary_text = self._clean_message_text(session.get("summary"))
				if not summary_text:
					continue

				ts = (
					self._normalize_timestamp(session.get("timestamp"))
					or self._normalize_timestamp(session.get("last_visit_time"))
					or now_iso
				)
				rows.append(
					{
						"id": session.get("id") or session_id,
						"created_at": ts,
						"namespace": ns,
						"session_id": session_id,
						"summary": summary_text[:200],
						"content": summary_text,
						"compressed_memory": summary_text,
						"category": "mid_term",
						"tags": [],
						"keywords": list(session.get("summary_keywords") or []),
						"retrieval_count": int(session.get("N_visit") or 0),
						"last_accessed": self._normalize_timestamp(session.get("last_visit_time")) or ts,
						"source": "memoryos",
					}
				)
				continue

			for page in pages:
				user_input = self._clean_message_text(page.get("user_input"))
				agent_response = self._clean_message_text(page.get("agent_response"))
				text = f"User: {user_input}\nAssistant: {agent_response}".strip()
				if not text:
					continue

				ts = self._normalize_timestamp(page.get("timestamp")) or now_iso
				rows.append(
					{
						"id": page.get("page_id"),
						"created_at": ts,
						"namespace": ns,
						"session_id": session_id,
						"summary": user_input[:200] if user_input else str(session.get("summary") or "").strip()[:200],
						"content": text,
						"compressed_memory": text,
						"category": "mid_term",
						"tags": [],
						"keywords": list(page.get("page_keywords") or []),
						"retrieval_count": int(session.get("N_visit") or 0),
						"last_accessed": self._normalize_timestamp(session.get("last_visit_time")) or ts,
						"source": "memoryos",
					}
				)

		for knowledge in storage.get_all_user_knowledge() or []:
			text = str(knowledge.get("text") or "").strip()
			if not text:
				continue
			rows.append(
				{
					"id": knowledge.get("id"),
					"created_at": now_iso,
					"namespace": ns,
					"session_id": None,
					"summary": text[:200],
					"content": text,
					"compressed_memory": text,
					"category": "user_knowledge",
					"tags": [],
					"keywords": [],
					"retrieval_count": 0,
					"last_accessed": None,
					"source": "memoryos",
				}
			)

		for knowledge in storage.get_all_assistant_knowledge() or []:
			text = str(knowledge.get("text") or "").strip()
			if not text:
				continue
			rows.append(
				{
					"id": knowledge.get("id"),
					"created_at": now_iso,
					"namespace": ns,
					"session_id": None,
					"summary": text[:200],
					"content": text,
					"compressed_memory": text,
					"category": "assistant_knowledge",
					"tags": [],
					"keywords": [],
					"retrieval_count": 0,
					"last_accessed": None,
					"source": "memoryos",
				}
			)

		rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
		return rows[:row_limit]

	def _load_memories(self) -> None:
		# MemoryOS manages persistence via Chroma + metadata JSON.
		return

	def _save_memories(self, **kwargs: Any) -> None:
		# Force metadata flush after store for process-safe visibility.
		self.memory_system.storage_provider.save_all_metadata()

	def _extract_user_assistant_pairs(self, messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
		cleaned_messages = [m for m in messages if isinstance(m, dict)]
		if not cleaned_messages:
			return []

		if str(cleaned_messages[0].get("role") or "") == "system":
			cleaned_messages = cleaned_messages[1:]

		pairs: list[tuple[str, str]] = []
		i = 0
		while i < len(cleaned_messages):
			current = cleaned_messages[i]
			role = str(current.get("role") or "")

			if role == "user":
				user_input = self._default_extract_text(current.get("content")).strip()
				agent_response = ""
				if i + 1 < len(cleaned_messages) and str(cleaned_messages[i + 1].get("role") or "") == "assistant":
					agent_response = self._default_extract_text(cleaned_messages[i + 1].get("content")).strip()
					i += 1
				if user_input or agent_response:
					pairs.append((user_input, agent_response))

			elif role == "assistant":
				# Handle occasional out-of-order adjacent assistant/user messages.
				if i + 1 < len(cleaned_messages) and str(cleaned_messages[i + 1].get("role") or "") == "user":
					user_input = self._default_extract_text(cleaned_messages[i + 1].get("content")).strip()
					agent_response = self._default_extract_text(current.get("content")).strip()
					if user_input or agent_response:
						pairs.append((user_input, agent_response))
					i += 1

			i += 1

		return pairs

	def _normalize_timestamp(self, raw_ts: Any) -> str | None:
		text = str(raw_ts or "").strip()
		if not text:
			return None

		if len(text) == 12 and text.isdigit():
			try:
				dt = datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
				return dt.isoformat()
			except ValueError:
				return text

		return text

	def _clean_message_text(self, raw: Any) -> str:
		text = str(raw or "")
		# Strip sender metadata wrapper from OpenClaw messages.
		text = re.sub(
			r"Sender \(untrusted metadata\):\s*```json[\s\S]*?```\s*",
			"",
			text,
			flags=re.IGNORECASE,
		)
		# Strip leading bracket timestamp like: [Sun 2026-03-15 00:03 GMT+8]
		text = re.sub(
			r"^\s*\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[+-]\d{1,2}(?::?\d{2})?\]\s*",
			"",
			text,
		)
		return text.strip()
