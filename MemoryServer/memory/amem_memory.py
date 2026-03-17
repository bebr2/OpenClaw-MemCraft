from __future__ import annotations

import json
import logging
import os
import pickle
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .A_mem.agentic_memory.memory_system import AgenticMemorySystem
from .base_memory import BaseMemory
from .llm_task_client import LLMTaskClient


logger = logging.getLogger(__name__)


class AMemMemory(BaseMemory):

    def __init__(
        self,
        data_dir: str,
        llm_client: LLMTaskClient | None = None,
        retrieve_model: str = "all-MiniLM-L6-v2",
        default_top_k: int = 5,
        context_max_chars: int = 4000,
    ) -> None:
        self.memory_cache_dir = Path(data_dir)
        self.memory_cache_dir.mkdir(parents=True, exist_ok=True)
        
        self.memory_system = AgenticMemorySystem(
            model_name=retrieve_model,
        )

        self.llm_client = llm_client
        self.default_top_k = max(1, int(default_top_k))
        self.context_max_chars = max(500, int(context_max_chars))

        self.documents: list[dict[str, Any]] = []

        self._load_memories()
        self.documents = self.list_memories(limit=100000)
        
    def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
        memory_map = getattr(self.memory_system, "memories", {}) or {}
        rows: list[dict[str, Any]] = []

        for note in memory_map.values():
            row = {
                "id": getattr(note, "id", None),
                "created_at": self._normalize_timestamp(getattr(note, "timestamp", None)),
                "namespace": namespace or "default",
                "session_id": None,
                "summary": str(getattr(note, "context", "") or "").strip(),
                "content": str(getattr(note, "content", "") or ""),
                "compressed_memory": str(getattr(note, "content", "") or ""),
                "category": getattr(note, "category", None),
                "tags": list(getattr(note, "tags", []) or []),
                "keywords": list(getattr(note, "keywords", []) or []),
                "retrieval_count": getattr(note, "retrieval_count", 0),
                "last_accessed": self._normalize_timestamp(getattr(note, "last_accessed", None)),
                "source": "a_mem",
            }
            rows.append(row)

        rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return rows[: max(1, int(limit))]
    
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

        stored_count = 0
        for msg in messages:
            role = str(msg.get("role") or "unknown")
            text = self._default_extract_text(msg.get("content")).strip()
            if not text:
                continue
            conversation_time = datetime.now(timezone.utc).isoformat()
            content = f"Speaker {role} says: {text}"
            self._add_memory(content=content, time=conversation_time)
            stored_count += 1

        self.documents = self.list_memories(limit=100000)
        self._save_memories()  # trigger save after batch store
            
        return {
            "stored": stored_count > 0,
            "stored_count": stored_count,
            "namespace": namespace,
            "session_id": session_id,
            "id": str(uuid.uuid4()),
        }

    def retrieve(
        self,
        conversation: dict[str, Any],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        q = self._default_extract_retrieve_query(conversation)
        if not q:
            return {"query": "", "memory_context": ""}
        
        keywords = self._generate_query_llm(q)
        context = self.memory_system.find_related_memories_raw(keywords, k=max(1, int(top_k or self.default_top_k)))

        return {
            "query": q,
            "memory_context": context,
        }
        
    def _save_memories(self, **kwargs):
        memory_cache_file = os.path.join(
            self.memory_cache_dir, 
            f"memory_cache.pkl"
        )
        retriever_cache_file = os.path.join(
            self.memory_cache_dir, 
            f"retriever_cache.pkl"
        )
        retriever_cache_embeddings_file = os.path.join(
            self.memory_cache_dir, 
            f"retriever_cache_embeddings.npy"
        )
        os.makedirs(self.memory_cache_dir, exist_ok=True)
        with open(memory_cache_file, "wb") as fout:
            pickle.dump(self.memory_system.memories, fout)
        self.memory_system.retriever.save(retriever_cache_file, retriever_cache_embeddings_file)
        print(f"\nSuccessfully saved memory cache to {memory_cache_file}, total {len(self.memory_system.memories)}")
        
    def _load_memories(self):
        memory_cache_file = os.path.join(
            self.memory_cache_dir, 
            f"memory_cache.pkl"
        )
        retriever_cache_file = os.path.join(
            self.memory_cache_dir, 
            f"retriever_cache.pkl"
        )
        retriever_cache_embeddings_file = os.path.join(
            self.memory_cache_dir, 
            f"retriever_cache_embeddings.npy"
        )
        if not os.path.exists(memory_cache_file):
            print(f"Memory cache file {memory_cache_file} does not exist.")
            return

        print(f"Loading memory cache from {memory_cache_file}")
        with open(memory_cache_file, 'rb') as f:
            cached_memories = pickle.load(f)
        # Restore memories to agent
        self.memory_system.memories = cached_memories
        if os.path.exists(retriever_cache_file):
            print(f"Found retriever cache files:")
            print(f"  - Retriever cache: {retriever_cache_file}")
            print(f"  - Embeddings cache: {retriever_cache_embeddings_file}")
            self.memory_system.retriever = self.memory_system.retriever.load(retriever_cache_file,retriever_cache_embeddings_file)
        else:
            print(f"No retriever cache found at {retriever_cache_file}, loading from memory")
            self.memory_system.retriever = self.memory_system.retriever.load_from_local_memory(cached_memories, 'all-MiniLM-L6-v2')
        
    def _generate_query_llm(self, question):
        if self.llm_client is None:
            return question

        prompt = f"""Given the following question, generate several keywords, using 'cosmos' as the separator.

Question: {question}

Format your response as a JSON object with a "keywords" field containing the selected text. 

Example response format:
{{"keywords": "keyword1, keyword2, keyword3"}}"""
        # different from original A-mem:
        # add a retry mechanism to avoid potential issues with LLM response
        response = self.llm_client.get_llm_response(
                messages=[{"role": "user", "content": prompt}],
                schema={
                    "type": "json_schema", 
                    "json_schema": {
                        "name": "response",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "keywords": {
                                    "type": "string",
                                }
                            },
                            "required": ["keywords"],
                            "additionalProperties": False
                        },
                        "strict": True
                    }
                }
            )
        print(f"LLM response for query generation: {response}")
        try:
            keywords = json.loads(response)["keywords"]
            return keywords
        except Exception:
            return question
        
    def _add_memory(self, content, time=None):
        return self.memory_system.add_note(content, time=time)

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

    