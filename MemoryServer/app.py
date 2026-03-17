from __future__ import annotations

import logging
import os
import json
import uuid
import importlib
import inspect
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from memory.base_memory import BaseMemoryStore
from memory.llm_task_client import LLMTaskClient

load_dotenv()

LOG_LEVEL = (os.getenv("MEMORY_LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

HOST = os.getenv("MEMORY_SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("MEMORY_SERVER_PORT", "8765"))
DATA_DIR = os.getenv("MEMORY_DATA_DIR", "./data")
TOP_K = int(os.getenv("MEMORY_TOP_K", "5"))
CONTEXT_MAX_CHARS = int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "4000"))
MEMORY_STORE_MODULE = (os.getenv("MEMORY_STORE_MODULE") or "bm25_memory").strip()
MEMORY_STORE_CLASS = (os.getenv("MEMORY_STORE_CLASS") or "").strip()
MEMORY_RETRIEVE_MODEL=os.getenv("MEMORY_RETRIEVE_MODEL", "all-MiniLM-L6-v2").strip()


LLM_ENABLED = os.getenv("MEMORY_LLM_TASK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
LLM_GATEWAY_BASE_URL = os.getenv("MEMORY_LLM_GATEWAY_BASE_URL") or "http://127.0.0.1:18789"
LLM_GATEWAY_TOKEN = os.getenv("MEMORY_LLM_GATEWAY_TOKEN") or os.getenv("OPENCLAW_GATEWAY_TOKEN") or None
LLM_AGENT_ID = os.getenv("MEMORY_LLM_AGENT_ID") or "memory-compressor"
LLM_MODEL = os.getenv("MEMORY_LLM_MODEL") or "openclaw"
LLM_TIMEOUT_MS = int(os.getenv("MEMORY_LLM_TIMEOUT_MS", "120000"))
COMPRESS_PROMPT = os.getenv(
    "MEMORY_COMPRESS_PROMPT",
    "You are a memory compressor. Keep long-term useful facts, remove noise, and output summary/compressed_memory/tags.",
)

llm_client = None
if LLM_ENABLED:
    llm_client = LLMTaskClient(
        gateway_base_url=LLM_GATEWAY_BASE_URL,
        gateway_token=LLM_GATEWAY_TOKEN,
        agent_id=LLM_AGENT_ID,
        model=LLM_MODEL,
        timeout_ms=LLM_TIMEOUT_MS,
    )


def _resolve_memory_module_path(module_name: str) -> str:
    normalized = (module_name or "").strip()
    if not normalized:
        raise RuntimeError("MEMORY_STORE_MODULE cannot be empty")
    if normalized.startswith("memory."):
        return normalized
    return f"memory.{normalized}"


def _select_store_class(module: Any, explicit_class_name: str | None = None) -> type[BaseMemoryStore]:
    if explicit_class_name:
        selected = getattr(module, explicit_class_name, None)
        if selected is None:
            raise RuntimeError(
                f"MEMORY_STORE_CLASS={explicit_class_name!r} not found in module {module.__name__!r}"
            )
        if not inspect.isclass(selected) or not issubclass(selected, BaseMemoryStore):
            raise RuntimeError(
                f"Configured store class {explicit_class_name!r} must inherit BaseMemoryStore"
            )
        return selected

    candidates: list[type[BaseMemoryStore]] = []
    for _, member in inspect.getmembers(module, inspect.isclass):
        if member is BaseMemoryStore:
            continue
        if issubclass(member, BaseMemoryStore) and member.__module__ == module.__name__:
            candidates.append(member)

    if not candidates:
        raise RuntimeError(
            f"No BaseMemoryStore implementation found in module {module.__name__!r}"
        )

    if len(candidates) > 1:
        names = ", ".join(sorted(cls.__name__ for cls in candidates))
        raise RuntimeError(
            f"Multiple store classes found in {module.__name__!r}: {names}. "
            "Set MEMORY_STORE_CLASS to pick one."
        )

    return candidates[0]


def _build_store_init_kwargs(store_cls: type[BaseMemoryStore]) -> dict[str, Any]:
    available_kwargs = {
        "data_dir": DATA_DIR,
        "llm_client": llm_client,
        "default_top_k": TOP_K,
        "context_max_chars": CONTEXT_MAX_CHARS,
        "llm_prompt_template": COMPRESS_PROMPT,
        "retrieve_model": MEMORY_RETRIEVE_MODEL,
    }

    signature = inspect.signature(store_cls.__init__)
    parameters = signature.parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    if accepts_kwargs:
        return available_kwargs

    filtered_kwargs: dict[str, Any] = {}
    for name in available_kwargs:
        if name in parameters:
            filtered_kwargs[name] = available_kwargs[name]
    return filtered_kwargs


def _load_memory_store() -> BaseMemoryStore:
    module_path = _resolve_memory_module_path(MEMORY_STORE_MODULE)
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to import memory store module {module_path!r}: {exc}") from exc

    store_class = _select_store_class(module, explicit_class_name=MEMORY_STORE_CLASS or None)
    init_kwargs = _build_store_init_kwargs(store_class)

    try:
        instance = store_class(**init_kwargs)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialize memory store {store_class.__name__!r} from module {module_path!r}: {exc}"
        ) from exc

    if not isinstance(instance, BaseMemoryStore):
        raise RuntimeError(
            f"Loaded memory store instance {store_class.__name__!r} does not implement BaseMemoryStore"
        )

    logger = logging.getLogger("memoryserver.app")
    logger.info(
        "[memory.store_loader] loaded module=%s class=%s",
        module_path,
        store_class.__name__,
    )
    return instance


store = _load_memory_store()

app = FastAPI(title="MemCraft MemoryServer", version="0.1.0")
logger = logging.getLogger("memoryserver.app")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "host": HOST,
        "port": PORT,
        "memory_store_module": MEMORY_STORE_MODULE,
        "memory_store_class": store.__class__.__name__,
        "llm_task_enabled": LLM_ENABLED,
        "stats": store.stats(),
    }


@app.post("/retrieve")
def retrieve(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:8]
    top_k = int(payload.get("top_k") or TOP_K)
    session_id = payload.get("session_id")
    namespace = payload.get("namespace")
    incoming_messages = payload.get("messages")

    conversation = {
        "prompt": payload.get("prompt") or payload.get("query") or "",
        "query": payload.get("query") or "",
        "messages": incoming_messages if isinstance(incoming_messages, list) else [],
        "session_id": session_id,
        "agent_id": payload.get("agent_id"),
        "namespace": namespace,
    }

    wrapped_query = {
        "conversation": {
            "prompt": str(conversation.get("prompt") or "")[:500],
            "query": str(conversation.get("query") or "")[:500],
            "messages_count": len(conversation.get("messages") or []),
        },
        "top_k": top_k,
        "filters": {"session_id": session_id, "namespace": namespace},
    }
    logger.info(
        "[memory.retrieve] rid=%s wrapped=%s",
        request_id,
        json.dumps(wrapped_query, ensure_ascii=False),
    )

    result = store.retrieve(
        conversation=conversation,
        top_k=top_k,
        filters={"session_id": session_id, "namespace": namespace},
    )

    if bool(payload.get("debug_render_prompt")):
        prompt_title = str(payload.get("prompt_title") or "Related memory context")
        base_prompt = str(payload.get("base_prompt") or conversation.get("prompt") or "")
        memory_block = store.render_memory_block(prompt_title, result.get("memory_context") or "")
        final_prompt = (f"{memory_block}\n\n{base_prompt}".strip() if memory_block else base_prompt)
        result["debug"] = {
            "prompt_title": prompt_title,
            "memory_block_preview": memory_block,
            "final_prompt_preview": final_prompt,
        }

    logger.info(
        "[memory.retrieve] rid=%s result context_chars=%s context_preview=%s",
        request_id,
        len(result.get("memory_context") or ""),
        str(result.get("memory_context") or "")[:200],
    )
    return result


@app.post("/store")
def persist(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:8]
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty list")

    conversation = {
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "namespace": payload.get("namespace") or "default",
        "trigger": payload.get("trigger") or "unknown",
        "messages": messages,
    }

    metadata = {
        "compress_prompt": payload.get("compress_prompt") or COMPRESS_PROMPT,
    }

    logger.info(
        "[memory.store] rid=%s trigger=%s session=%s namespace=%s message_count=%s",
        request_id,
        conversation.get("trigger"),
        conversation.get("session_id"),
        conversation.get("namespace"),
        len(conversation.get("messages") or []),
    )
    logger.info(
        "[memory.store] rid=%s precompress_messages=%s",
        request_id,
        json.dumps(conversation.get("messages") or [], ensure_ascii=False),
    )

    result = store.store(conversation=conversation, metadata=metadata)
    logger.info("[memory.store] rid=%s result=%s", request_id, json.dumps(result, ensure_ascii=False))
    return result


@app.get("/memories")
def memories(limit: int = 200, namespace: str | None = None) -> dict[str, Any]:
    payload = store.build_list_memories_payload(limit=limit, namespace=namespace)
    return {
        "schema_version": payload.get("schema_version", 1),
        "store_type": payload.get("store_type", store.__class__.__name__),
        "items": payload.get("items", []),
        "raw_items": payload.get("raw_items", []),
        "stats": store.stats(),
    }


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(str(Path(__file__).parent / "web" / "index.html"))


if __name__ == "__main__":
    import uvicorn

    # Pass the app object directly to avoid re-importing this module and
    # double-initializing the memory store when launched via `python app.py`.
    uvicorn.run(app, host=HOST, port=PORT, reload=False)
