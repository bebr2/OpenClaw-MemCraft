[English](README.md)

<h1 align="center">MemCraft : DIY 你的 OpenClaw 记忆系统</h1>


<h2 align="center"><i>Build your own memory. </i></h2>

<p align="center">
	<img src="https://img.shields.io/badge/协议-Apache%202.0-green" alt="License" />
	<img src="https://img.shields.io/badge/代码-Python%20%7C%20JavaScript-orange" alt="Languages" />
	<img src="https://img.shields.io/badge/OpenClaw-Plugin%20%7C%20Lifecycle-purple" alt="OpenClaw Plugin Type" />
</p>



*MemCraft 是一个 OpenClaw 的记忆系统集成插件，目的是将 LLM Memory 领域的重要 baseline 接入 OpenClaw，提供一个本地优先、可复现实验、可扩展开发的统一记忆框架。*

## MemCraft 能做什么

### 面向普通用户
- 可以直接在本地使用开源的 memory 系统。
- 自主选择不是依赖云端记忆服务，控制隐私风险。
- 可以按需求自主 DIY 新的 memory 系统。

### 面向研究者
- 科研提出的 memory baseline 便捷接入 OpenClaw。
- 在 OpenClaw 真实交互场景下评测 baseline 表现。
- 可以基于统一接口快速发现现有方案不足并改进。

## 项目结构

- MemCraft/: OpenClaw 生命周期插件（Node.js）
- MemoryServer/: 记忆后端服务

## 已实现的 Memory Store Baseline

- bm25_memory: 轻量本地 BM25 + LLM 压缩存储
- amem_memory: [A-Mem](https://github.com/agiresearch/A-mem)
- mem0_memory: [mem0](https://github.com/mem0ai/mem0)
- memoryos_memory: [MemoryOS](https://github.com/BAI-LAB/MemoryOS)

Baseline 的实现参照 [MemoryBench](https://github.com/LittleDinoC/MemoryBench/) 的复现

## 快速开始

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

默认地址: http://127.0.0.1:8765

### 3. Install Plugin to OpenClaw

#### npm 安装（推荐）

```bash
openclaw plugins install memcraft-openclaw-plugin@latest
```


#### 手动安装

将`./memcraft-openclaw-plugin` 整个文件夹复制到 OpenClaw 插件目录，通常为 `~/.openclaw/extensions/`，然后在 OpenClaw 的插件配置（通常为 `~/.openclaw/openclaw.json`）中添加：

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


以上两种安装方式，均需要在`~/.openclaw/openclaw.json` 中添加：

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

然后重启网关：

```bash
openclaw gateway restart
```

## 环境配置

### MemoryServer baseline configs

- MemoryServer/.env.bm25
- MemoryServer/.env.amem
- MemoryServer/.env.mem0
- MemoryServer/.env.memoryos

### OpenClaw plugin config

- MemCraft/.env.memcraft-plugin

请将插件的环境配置添加到 OpenClaw 的 .env 文件中，路径通常为 ~/.openclaw/.env，如果文件不存在则新建，修改插件配置同样需要重启 OpenClaw 网关。

## 重要的环境变量

### MemoryServer

- MEMORY_STORE_MODULE: 选择 baseline（bm25_memory/amem_memory/mem0_memory/memoryos_memory）
- MEMORY_SERVER_HOST, MEMORY_SERVER_PORT: 服务监听地址
- MEMORY_DATA_DIR: 记忆数据目录
- MEMORY_TOP_K: 检索条数
- MEMORY_RETRIEVE_MODEL: 向量检索模型（适用于支持 embedding 的 baseline）
- MEMORY_LLM_GATEWAY_BASE_URL, MEMORY_LLM_GATEWAY_TOKEN: 网关地址与鉴权
- MEMORY_LLM_AGENT_ID: 压缩 agent 标识（建议与插件排除列表配合）

### Plugin (MemCraft)

- MEMCRAFT_SERVER_URL: 后端地址
- MEMCRAFT_TOP_K: 请求检索条数
- MEMCRAFT_STORE_GRANULARITY: session_end 或 agent_end，代表记忆存储的粒度（即在何时触发，session 结束或 turn 结束）
- MEMCRAFT_NAMESPACE: 记忆命名空间
- MEMCRAFT_EXCLUDE_AGENT_IDS: 排除不参与记忆的 agent（如 memory-compressor）
- MEMCRAFT_STRIP_HISTORY_MEMORY: 是否剥离历史注入 memory 块，默认值为 True，可以节省 token，memory只保留本次对话相关的内容

## MemCraft 工作流

1. OpenClaw 在 before_prompt_build 调插件。
2. 插件调用 POST /retrieve 获取 memory_context。
3. memory_context 注入 prompt 后再进入主模型。
4. 在 session_end 或 agent_end，插件调用 POST /store。
5. 后端压缩并持久化记忆。

## 前端 Dashboard

MemoryServer 内置一个轻量前端页面，便于观察记忆状态和调试：

- 访问地址: GET /
- 数据来源: GET /memories

页面包含：

- 总文档数（Total Documents）
- 平均文档长度（Avg Doc Length）
- 近 7 天记忆总数与柱状趋势图
- 最近记忆列表（含 session/source/time/category 等元信息，视具体 store 实现而定）


## 如何 DIY 一个新的 Memory Store

DIY 新 memory store 可以让你可以把自己的记忆算法接到 OpenClaw 实际对话链路里，例如：

- 研究场景: 对比不同检索策略在真实多轮会话中的效果
- 工程场景: 替换存储后端（JSONL/SQLite/向量库/图存储），或者将自定义存储间隔（例如多轮对话结束时存储，而非每轮或每个session）
- 产品场景: 增加领域特化记忆（任务记忆、用户画像记忆、工作流状态记忆）

插件不需要知道你内部怎么做记忆，只要你的 store 实现了统一接口，整个系统就能跑。

### 函数接口

系统调用链路如下:

1. OpenClaw 插件在对话前调用 POST /retrieve
2. MemoryServer 路由层调用 store.retrieve(...)
3. 插件把返回的 memory_context 注入 prompt
4. 回合结束后插件调用 POST /store
5. MemoryServer 路由层调用 store.store(...)
6. 仪表盘/调试页调用 GET /memories
7. MemoryServer 路由层调用 store.list_memories(...)

对应到要实现的方法:

- retrieve(conversation, top_k, filters)
作用: 根据当前对话提取查询并返回可注入 prompt 的文本，conversation["prompt"] 包含即将发送给 LLM 的完整 prompt，conversation["messages"] 包含历史消息列表。
被谁调用: POST /retrieve 路由
最关键返回: query, memory_context

- store(conversation, metadata)
作用: 把当前会话内容持久化，conversation["messages"] 包含上次存储至今的消息列表。
被谁调用: POST /store 路由
最关键返回: stored=true/false, item_id 或等价标识

- list_memories(limit, namespace)
作用: 给前端和调试接口展示当前记忆条目
被谁调用: GET /memories 路由
建议: 返回统一字典列表，至少包含时间、内容、命名空间等信息

- _load_memories()
作用: 进程启动时把已有记忆加载进内存或索引
被谁调用: 你的 store 初始化阶段
常见实现: 从 jsonl/sqlite/vector db 恢复索引

- _save_memories(...)
作用: store 写入后把变更落盘或刷入后端
被谁调用: 通常在 store 内部调用
常见实现: append jsonl、写 sqlite、写向量库并 flush

### 最小可运行模板

在 MemoryServer/memory 下新建 your_memory.py:

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base_memory import BaseMemoryStore


class YourMemoryStore(BaseMemoryStore):
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

### 配置加载

在 MemoryServer/.env 设置:

- MEMORY_STORE_MODULE=your_memory
- MEMORY_STORE_CLASS=YourMemoryStore（仅当同模块有多个 store 类时需要指定）

然后重启服务。

### 实现建议

- retrieve 返回内容要短且可读，避免把整个历史原样塞回 prompt
- store 要做噪声过滤和去重，否则记忆会迅速污染
- list_memories 尽量返回稳定字段，方便 UI 展示和调试
- _load/_save 要考虑异常恢复，不要因为一条坏数据导致全量加载失


### 最小化验证清单

1. GET /health 返回 ok=true
2. 对话时触发 POST /retrieve
3. 回合结束后触发 POST /store
4. GET /memories 能看到写入
