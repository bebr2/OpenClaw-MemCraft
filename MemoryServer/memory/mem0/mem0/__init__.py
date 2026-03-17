import importlib.metadata

try:
	__version__ = importlib.metadata.version("mem0ai")
except importlib.metadata.PackageNotFoundError:
	__version__ = "0.0.0-local"

try:
	from mem0.client.main import AsyncMemoryClient, MemoryClient  # noqa
except Exception:  # pragma: no cover - optional in local vendored mode
	AsyncMemoryClient = None  # type: ignore[assignment]
	MemoryClient = None  # type: ignore[assignment]

from mem0.memory.main import AsyncMemory, Memory  # noqa
