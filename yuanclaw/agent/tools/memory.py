"""Memory recall tools for YuanClaw."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.memory import CoreMemoryBackend, MemoryBackend


def _resolve_backend(workspace: Path | None, backend: MemoryBackend | None) -> MemoryBackend:
    if backend is not None:
        return backend
    resolved_workspace = Path(workspace or Path.cwd()).expanduser().resolve()
    return CoreMemoryBackend(resolved_workspace)


class MemorySearchTool(Tool):
    """Search workspace memory with citations."""

    def __init__(self, workspace: Path | None = None, backend: MemoryBackend | None = None) -> None:
        self._workspace = Path(workspace or getattr(backend, "workspace", Path.cwd())).expanduser().resolve()
        self._backend = _resolve_backend(self._workspace, backend)

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return "Search YuanClaw memory files and return cited snippets."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                    "minLength": 1,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of hits to return",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 8, **kwargs: Any) -> str:
        try:
            hits = self._backend.search(query, max_results=max_results)
        except Exception as exc:
            return f"Error searching memory: {exc}"

        if not hits:
            return f"No memory hits for: {query}"

        lines = [f"Memory hits for: {query}"]
        for idx, hit in enumerate(hits, start=1):
            lines.append(f"{idx}. {hit.citation} (score={hit.score:.2f})")
            if hit.snippet:
                lines.append(f"   {hit.snippet}")
        return "\n".join(lines)


class MemoryGetTool(Tool):
    """Read a cited slice from a memory document."""

    def __init__(self, workspace: Path | None = None, backend: MemoryBackend | None = None) -> None:
        self._workspace = Path(workspace or getattr(backend, "workspace", Path.cwd())).expanduser().resolve()
        self._backend = _resolve_backend(self._workspace, backend)

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def description(self) -> str:
        return "Read a YuanClaw memory document or cited line range."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Memory file path, relative to the workspace when possible",
                    "minLength": 1,
                },
                "start_line": {
                    "type": ["integer", "null"],
                    "description": "Optional starting line",
                    "minimum": 1,
                },
                "end_line": {
                    "type": ["integer", "null"],
                    "description": "Optional ending line",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            doc = self._backend.get(path, start_line=start_line, end_line=end_line)
        except Exception as exc:
            return f"Error reading memory: {exc}"

        header = f"{doc.citation}"
        body = doc.render_with_lines()
        if not body:
            return f"{header}\n(empty)"
        return f"{header}\n{body}"
