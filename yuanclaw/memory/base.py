"""Memory backend primitives for YuanClaw."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A cited memory search result."""

    path: str
    line: int
    snippet: str
    score: float = 0.0

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True, slots=True)
class MemoryDoc:
    """A document slice returned by memory.get()."""

    path: str
    start_line: int
    end_line: int
    lines: list[str]

    @property
    def citation(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"

    @property
    def content(self) -> str:
        return "\n".join(self.lines)

    def render_with_lines(self) -> str:
        return "\n".join(
            f"{line_no} | {line}" for line_no, line in enumerate(self.lines, start=self.start_line)
        )


class MemoryBackend(Protocol):
    """Backend contract for YuanClaw memory storage and recall."""

    workspace: Path

    def build_context(
        self,
        session_key: str,
        channel: str,
        chat_id: str,
        is_group: bool = False,
    ) -> str:
        """Return the memory context to inject into the prompt."""

    def search(self, query: str, max_results: int = 8) -> list[MemoryHit]:
        """Search memory documents and return cited hits."""

    def get(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> MemoryDoc:
        """Return a cited slice from a memory document."""

    def status(self) -> dict[str, Any]:
        """Return backend status for debugging and diagnostics."""
