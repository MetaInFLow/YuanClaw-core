"""Legacy memory backend for YuanClaw."""

from __future__ import annotations

from pathlib import Path

from yuanclaw.memory.core import CoreMemoryBackend


class LegacyMemoryBackend(CoreMemoryBackend):
    """Compatibility backend that keeps the existing MEMORY.md/HISTORY.md model."""

    backend_name = "legacy"

    def __init__(self, workspace: Path):
        super().__init__(workspace, daily_pages=False, recent_days=0, extra_paths=None)

    def build_context(
        self,
        session_key: str,
        channel: str,
        chat_id: str,
        is_group: bool = False,
    ) -> str:
        if is_group:
            return ""

        memory_doc = self.get("memory/MEMORY.md") if (self.memory_dir / "MEMORY.md").exists() else None
        if memory_doc is None:
            return ""
        return self._format_section("Long-term Memory", memory_doc)

    def _indexed_paths(self) -> list[Path]:
        paths: list[Path] = []
        for candidate in (
            self.memory_dir / "MEMORY.md",
            self.memory_dir / "HISTORY.md",
        ):
            if candidate.exists():
                paths.append(candidate.resolve())

        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique
