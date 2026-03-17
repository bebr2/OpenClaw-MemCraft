from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
from tqdm import tqdm
from .base_memory import BaseMemoryStore
from .llm_task_client import LLMTaskClient
from concurrent.futures import ThreadPoolExecutor, as_completed

# Use vendored mem0 source under memory/mem0 when package mem0 is not installed.
_MEM0_VENDOR_ROOT = Path(__file__).resolve().parent / "mem0"
if str(_MEM0_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEM0_VENDOR_ROOT))

from mem0.configs.base import MemoryConfig
from mem0.embeddings.configs import EmbedderConfig
from mem0.llms.configs import LlmConfig
from mem0.memory.main import Memory
from mem0.memory.main import _build_filters_and_metadata
from mem0.vector_stores.configs import VectorStoreConfig

logger = logging.getLogger(__name__)


class Mem0MemoryStore(BaseMemoryStore):
    """Mem0-backed memory store with local Chroma persistence."""

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

        self.mem0_dir = self.data_dir / "mem0"
        self.mem0_dir.mkdir(parents=True, exist_ok=True)

        self.default_top_k = max(1, int(default_top_k))
        self.context_max_chars = max(500, int(context_max_chars))

        # Keep this argument for constructor compatibility with app bootstrap.
        self.llm_client = llm_client

        self.user_id = os.getenv("MEMORY_MEM0_USER_ID", "default-user")
        self._build_memory_system(retrieve_model=retrieve_model)

        self.documents: list[dict[str, Any]] = []
        self._load_memories()

    def _build_memory_system(self, retrieve_model: str) -> None:
        embedder_config = EmbedderConfig(
            provider="huggingface",
            config={"model": retrieve_model},
        )

        vector_store_config = VectorStoreConfig(
            provider="chroma",
            config={
                "collection_name": "memcraft_mem0",
                "path": str(self.mem0_dir / "chroma"),
            },
        )

        # mem0 add/search works with infer=False; provide a dummy key so client init stays quiet.
        llm_config = LlmConfig(provider="openai", config={"api_key": os.getenv("OPENAI_API_KEY", "dummy")})
        memory_config = MemoryConfig(
            llm=llm_config,
            embedder=embedder_config,
            vector_store=vector_store_config,
            history_db_path=str(self.mem0_dir / "history.db"),
        )

        self.memory_system = Memory(memory_config)
        self.memory_system.llm = self.llm_client

    def retrieve(
        self,
        conversation: dict[str, Any],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = self._default_extract_retrieve_query(conversation)
        if not query:
            return {"query": "", "memory_context": ""}

        results = self.memory_system.search(
            query=query,
            user_id=self.user_id,
            filters=None,
            limit=max(1, int(top_k or self.default_top_k)),
        )
        memories_str = "\n".join(f"- {entry['memory']}" for entry in results["results"])
        
        return {
            "query": query,
            "memory_context": memories_str,
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

        if self.llm_client is None:
            return {
                "stored": False,
                "skipped": True,
                "reason": "llm_client_required_when_infer_true",
            }

        self.memory_system.llm = self.llm_client

        merged_metadata: dict[str, Any] = {
            "namespace": namespace,
            "session_id": session_id,
            **(metadata or {}),
        }

        cnt = 100
        while cnt:
            cnt -= 1
            try:
                print(f"[Mem0] Attempting to add memory for session {session_id} with {len(messages)} messages. Retries left: {cnt}")
                self.memory_system.add(
                    messages,
                    user_id=self.user_id,
                    run_id=session_id,
                    metadata=merged_metadata,
                    infer=True,
                )
                print(f"[Mem0] Successfully added memory for session {session_id} with {len(messages)} messages.")
                break
            except Exception as e:
                print(f"[Mem0] Error adding memory, retrying... {e}")
                if cnt == 0:
                    return {
                        "stored": False,
                        "skipped": True,
                        "reason": f"failed to add memory after retries: {e}",
                    }
        return {
            "stored": True,
            "namespace": namespace,
            "session_id": session_id,
            "id": str(uuid.uuid4()),
        }

    def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
        row_limit = max(1, int(limit))

        result = self.memory_system.get_all(
            user_id=self.user_id,
            limit=row_limit,
        )
        rows = result.get("results") if isinstance(result, dict) else []

        memories: list[dict[str, Any]] = []
        for row in rows or []:
            row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

            row_namespace = row_metadata.get("namespace") or namespace or "default"
            if namespace and row_namespace != namespace:
                continue

            session_id = (
                row.get("run_id")
                or row_metadata.get("run_id")
                or row_metadata.get("session_id")
            )

            memory_text = str(row.get("memory") or "").strip()
            memories.append(
                {
                    "id": row.get("id"),
                    "created_at": self._normalize_timestamp(row.get("created_at")),
                    "namespace": row_namespace,
                    "session_id": session_id,
                    "summary": memory_text,
                    "content": memory_text,
                    "compressed_memory": memory_text,
                    "category": row_metadata.get("category"),
                    "tags": list(row_metadata.get("tags") or []),
                    "keywords": list(row_metadata.get("keywords") or []),
                    "retrieval_count": row_metadata.get("retrieval_count", 0),
                    "last_accessed": self._normalize_timestamp(row_metadata.get("last_accessed")),
                    "source": "mem0",
                }
            )

        memories.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return memories[:row_limit]


    def _save_memories(self, **kwargs: Any) -> None:
        # mem0 persists internally (vector store + history db); no extra snapshot needed.
        return

    def _load_memories(self) -> None:
        def embed(data: str, action: str):
            return self.memory_system.embedding_model.embed(data, action)

        cursor = self.memory_system.db.connection.cursor()
        cursor.execute("SELECT * FROM history")
        col_names = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        rows = [dict(zip(col_names, row)) for row in rows]

        if not rows:
            print("[Mem0] No memories to load from DB.")
            return

        print(f"[Mem0] Loading {len(rows)} memories from DB into vector store...")

        def solve_row(row):
            cnt = 20 
            while cnt:
                try:
                    data = row["new_memory"]
                    memory_id = row["memory_id"]
                    metadata = {
                        "data": data,
                        "hash": hashlib.md5(data.encode()).hexdigest(),
                    }
                    for key in ["created_at", "updated_at", "user_id", "agent_id", "run_id", "actor_id", "role"]:
                        if key in row and row[key] is not None:
                            metadata[key] = row[key]
                    metadata, filters = _build_filters_and_metadata(
                        user_id=self.user_id,
                        input_metadata=metadata,
                    )
                    if row["event"] == "ADD":
                        self.memory_system.vector_store.insert(
                            vectors=[embed(data, action="add")],
                            ids=[memory_id],
                            payloads=[metadata],
                        )
                    elif row["event"] == "UPDATE":
                        # for key in ["updated_at", "user_id", "agent_id", "run_id", "actor_id", "role"]:
                        #     if key in row:
                        #         metadata[key] = row[key]
                        self.memory_system.vector_store.update(
                            vector_id=memory_id,
                            vector=embed(data, action="update"),
                            payload=metadata,
                        )
                    elif row["event"] == "DELETE":
                        self.memory_system.vector_store.delete(vector_id=memory_id)
                    break
                except Exception as e:
                    cnt -= 1

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(solve_row, row): row for row in rows}
            for future in tqdm(as_completed(futures), total=len(rows), desc="Loading memories"):
                try:
                    future.result()  # Raise any exceptions that occurred
                except Exception as e:
                    print(f"Error processing row {futures[future]}: {e}")

        print("[Mem0] Finished loading memories into vector store.")

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
