[中文](README_zh.md)

<h1 align="center">MemCraft: DIY Your OpenClaw Memory System</h1>

<h2 align="center"><i>Build your own memory.</i></h2>

<p align="center">
	<img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License" />
	<img src="https://img.shields.io/badge/Code-Python%20%7C%20JavaScript-orange" alt="Languages" />
	<img src="https://img.shields.io/badge/OpenClaw-Plugin%20%7C%20Lifecycle-purple" alt="OpenClaw Plugin Type" />
</p>

*MemCraft is an OpenClaw memory integration plugin. Its goal is to connect major LLM Memory baselines to OpenClaw and provide a local-first, reproducible, and extensible unified memory framework.*

# Table of Contents

- [Table of Contents](#table-of-contents)
	- [What MemCraft Does](#what-memcraft-does)
		- [For General Users](#for-general-users)
		- [For Researchers](#for-researchers)
	- [Project Structure](#project-structure)
	- [Implemented Memory Baselines](#implemented-memory-baselines)
	- [Quick Start](#quick-start)
		- [1. Clone](#1-clone)
		- [2. Start MemoryServer](#2-start-memoryserver)
		- [3. Install Plugin to OpenClaw](#3-install-plugin-to-openclaw)
			- [npm Install (Recommended)](#npm-install-recommended)
			- [Manual Install](#manual-install)
	- [Environment Configuration](#environment-configuration)
		- [MemoryServer baseline configs](#memoryserver-baseline-configs)
		- [OpenClaw plugin config](#openclaw-plugin-config)
	- [Important Environment Variables](#important-environment-variables)
		- [MemoryServer](#memoryserver)
		- [Plugin (MemCraft)](#plugin-memcraft)
	- [MemCraft Workflow](#memcraft-workflow)
	- [Frontend Dashboard](#frontend-dashboard)
	- [DIY: Build a New Memory](#diy-build-a-new-memory)
		- [Function Interface](#function-interface)
		- [Minimal Runnable Template](#minimal-runnable-template)
		- [Configuration Loading](#configuration-loading)
		- [Implementation Suggestions](#implementation-suggestions)
		- [Minimal Validation Checklist](#minimal-validation-checklist)

## What MemCraft Does

### For General Users
- Use open-source memory systems locally.
- Reduce privacy risks by not relying on cloud memory services.
- DIY your own memory system based on your needs.

### For Researchers
- Quickly plug in memory baselines proposed in research.
- Evaluate baseline performance in real OpenClaw interaction scenarios.
- Identify limitations of existing methods through a unified interface and improve them.

## Project Structure

- MemCraft/: OpenClaw lifecycle plugin (Node.js)
- MemoryServer/: memory backend service

## Implemented Memory Baselines

- bm25_memory: lightweight local BM25 + LLM-compressed storage
- amem_memory: [A-Mem](https://github.com/agiresearch/A-mem)
- mem0_memory: [mem0](https://github.com/mem0ai/mem0)
- memoryos_memory: [MemoryOS](https://github.com/BAI-LAB/MemoryOS)

The baseline implementations are reproduced with reference to [MemoryBench](https://github.com/LittleDinoC/MemoryBench/).

## Quick Start

### 1. Clone

```bash
git clone https://github.com/bebr2/OpenClaw-MemCraft.git
cd MemCraft
```

### 2. Start MemoryServer

```bash
cd MemoryServer
pip install -r requirements.txt
copy .env.bm25 .env
python app.py
```

Default URL: http://127.0.0.1:8765

### 3. Install Plugin to OpenClaw


#### npm Install (Recommended)

```bash
openclaw plugins install memcraft-openclaw-plugin@latest
```

#### Manual Install

Copy the entire `./memcraft-openclaw-plugin` folder into the OpenClaw extension directory (usually `~/.openclaw/extensions/`). Then add this in OpenClaw plugin config (usually `~/.openclaw/openclaw.json`):

```json
{
	"plugins": {
		"installs": {
			"memcraft-openclaw-plugin": {
				"source": "path",
				"installPath": "path/to/.openclaw/extensions/memcraft-openclaw-plugin"
			}
		},
		"entries": {
			"memcraft-openclaw-plugin": {
				"enabled": true
			}
		}
	}
}
```


For both install methods, ensure this exists in `~/.openclaw/openclaw.json`:

```json
{
	"gateway": {
		"http": {
			"endpoints": {
				"chatCompletions": {
					"enabled": true
				}
			}
		}
	},
}
```

Then restart gateway:

```bash
openclaw gateway restart
```

## Environment Configuration

### MemoryServer baseline configs

- MemoryServer/.env.bm25
- MemoryServer/.env.amem
- MemoryServer/.env.mem0
- MemoryServer/.env.memoryos

### OpenClaw plugin config

- MemCraft/.env.memcraft-plugin

Add plugin env configuration into OpenClaw `.env` (usually `~/.openclaw/.env`; create it if missing). Restart OpenClaw gateway after any plugin config change.

## Important Environment Variables

### MemoryServer

- MEMORY_STORE_MODULE: choose baseline (`bm25_memory/amem_memory/mem0_memory/memoryos_memory`)
- MEMORY_SERVER_HOST, MEMORY_SERVER_PORT: service bind address
- MEMORY_DATA_DIR: memory data directory
- MEMORY_TOP_K: retrieval size
- MEMORY_RETRIEVE_MODEL: embedding retrieval model (for stores that support embeddings)
- MEMORY_LLM_GATEWAY_BASE_URL, MEMORY_LLM_GATEWAY_TOKEN: gateway endpoint and auth
- MEMORY_LLM_AGENT_ID: compression agent id (recommended to align with plugin exclusion list)

### Plugin (MemCraft)

- MEMCRAFT_SERVER_URL: backend URL
- MEMCRAFT_TOP_K: retrieval size
- MEMCRAFT_STORE_GRANULARITY: `session_end` or `agent_end`, defines when memory storage is triggered
- MEMCRAFT_NAMESPACE: memory namespace
- MEMCRAFT_EXCLUDE_AGENT_IDS: exclude agents from memory hooks (for example `memory-compressor`)
- MEMCRAFT_STRIP_HISTORY_MEMORY: strip injected memory blocks from history; default true to save tokens and keep only current-turn-related memory

## MemCraft Workflow

1. OpenClaw triggers plugin at `before_prompt_build`.
2. Plugin calls `POST /retrieve` to get `memory_context`.
3. `memory_context` is injected into prompt before main model execution.
4. At `session_end` or `agent_end`, plugin calls `POST /store`.
5. Backend compresses and persists memory.

## Frontend Dashboard

MemoryServer includes a lightweight dashboard for memory inspection and debugging:

- Entry: `GET /`
- Data source: `GET /memories`

The dashboard includes:

- Total Documents
- Avg Doc Length
- Recent 7-day memory trend chart
- Recent memory list (session/source/time/category, depending on store implementation)

## DIY: Build a New Memory

DIY lets you connect your own memory algorithm into real OpenClaw conversation flow, for example:

- Research: compare retrieval strategies in real multi-turn conversations
- Engineering: replace storage backend (JSONL/SQLite/vector DB/graph DB), or customize storage intervals (for example store at multi-turn boundaries rather than each turn/session)
- Product: add domain-specific memory (task memory, user profile memory, workflow state memory)

The plugin does not need to know your internal implementation. As long as your store follows the interface contract, the system works.

### Function Interface

Call chain:

1. OpenClaw plugin calls `POST /retrieve` before conversation turn
2. MemoryServer route calls `store.retrieve(...)`
3. Plugin injects returned `memory_context` into prompt
4. At turn/session end, plugin calls `POST /store`
5. MemoryServer route calls `store.store(...)`
6. Dashboard/debug page calls `GET /memories`
7. MemoryServer route calls `store.list_memories(...)`

Required methods:

- `retrieve(conversation, top_k, filters)`
Purpose: extract query from current conversation and return prompt-injectable text. `conversation["prompt"]` contains full prompt for LLM; `conversation["messages"]` contains message history.
Called by: `POST /retrieve`
Key return fields: `query`, `memory_context`

- `store(conversation, metadata)`
Purpose: persist current conversation content. `conversation["messages"]` contains messages since last store event.
Called by: `POST /store`
Key return fields: `stored=true/false`, `item_id` (or equivalent id)

- `list_memories(limit, namespace)`
Purpose: provide memory list for dashboard/debug endpoints.
Called by: `GET /memories`
Suggestion: return stable dict rows including timestamp/content/namespace.

- `_load_memories()`
Purpose: load persisted memories into in-memory structures/indexes on startup.
Called by: store initialization.
Typical implementation: restore from jsonl/sqlite/vector DB.

- `_save_memories(...)`
Purpose: flush changes to persistent storage after store operations.
Called by: typically inside your `store` implementation.
Typical implementation: append jsonl, write sqlite, write vector DB and flush.

### Minimal Runnable Template

Create `your_memory.py` under `MemoryServer/memory`:

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base_memory import BaseMemory


class YourMemory(BaseMemory):
	def __init__(
		self,
		data_dir: str,
		llm_client=None,
		default_top_k: int = 5,
		context_max_chars: int = 4000,
		**kwargs,
	) -> None:
		self.data_dir = Path(data_dir)
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self.memory_file = self.data_dir / "your_memory.jsonl"

		self.llm_client = llm_client
		self.default_top_k = max(1, int(default_top_k))
		self.context_max_chars = max(500, int(context_max_chars))

		self.documents: list[dict[str, Any]] = []
		self._load_memories()

	def retrieve(
		self,
		conversation: dict[str, Any],
		top_k: int = 5,
		filters: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		query = self._default_extract_retrieve_query(conversation)
		if not query:
			return {"query": "", "memory_context": ""}

		namespace = (filters or {}).get("namespace")
		rows = self.documents
		if namespace:
			rows = [x for x in rows if x.get("namespace") == namespace]

		picked = list(reversed(rows))[: max(1, int(top_k or self.default_top_k))]
		context = "\n".join(f"- {x.get('summary', '')}" for x in picked).strip()
		if len(context) > self.context_max_chars:
			context = context[: self.context_max_chars] + "..."

		return {
			"query": query,
			"memory_context": context,
		}

	def store(self, conversation: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
		namespace = conversation.get("namespace") or "default"
		messages = conversation.get("messages") or []
		if not messages:
			return {"stored": False, "reason": "empty_messages"}

		text = "\n".join(str(m.get("content") or "") for m in messages).strip()
		item = {
			"id": str(uuid.uuid4()),
			"created_at": datetime.now(timezone.utc).isoformat(),
			"namespace": namespace,
			"session_id": conversation.get("session_id"),
			"summary": text[:200],
			"compressed_memory": text[:1000],
			"meta": metadata or {},
		}

		self.documents.append(item)
		self._save_memories(item=item)
		return {"stored": True, "item_id": item["id"]}

	def list_memories(self, limit: int = 200, namespace: str | None = None) -> list[dict[str, Any]]:
		rows = self.documents
		if namespace:
			rows = [x for x in rows if x.get("namespace") == namespace]
		rows = rows[-max(1, int(limit)):]
		return list(reversed(rows))

	def _load_memories(self) -> None:
		if not self.memory_file.exists():
			self.documents = []
			return
		rows: list[dict[str, Any]] = []
		with self.memory_file.open("r", encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					rows.append(json.loads(line))
				except json.JSONDecodeError:
					continue
		self.documents = rows

	def _save_memories(self, **kwargs: Any) -> None:
		item = kwargs["item"]
		with self.memory_file.open("a", encoding="utf-8") as f:
			f.write(json.dumps(item, ensure_ascii=False) + "\n")
```

### Configuration Loading

Set in `MemoryServer/.env`:

- `MEMORY_STORE_MODULE=your_memory`
- `MEMORY_STORE_CLASS=YourMemory` (required only if multiple store classes exist in one module)

Then restart the service.

### Implementation Suggestions

- Keep `retrieve` output concise and readable; avoid injecting full raw history back into prompt.
- Add noise filtering and deduplication in `store`, otherwise memory quality degrades quickly.
- Keep `list_memories` fields stable for better UI/debug compatibility.
- Add resilience in `_load/_save` so one corrupt row does not break full load.

### Minimal Validation Checklist

1. `GET /health` returns `ok=true`
2. `POST /retrieve` is triggered during conversation
3. `POST /store` is triggered at turn/session end
4. `GET /memories` shows persisted rows
