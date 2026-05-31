"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from yuanclaw.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from yuanclaw.providers.base import LLMProvider
    from yuanclaw.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + daily pages + HISTORY.md legacy log."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    @staticmethod
    def _today_key() -> str:
        """Return the current day key used for daily memory pages."""
        return datetime.now().strftime("%Y-%m-%d")

    def _daily_page_path(self, day_key: str | None = None) -> Path:
        """Return the daily page path for a given day key."""
        return self.memory_dir / f"{day_key or self._today_key()}.md"

    @staticmethod
    def _format_daily_entry(entry: str) -> str:
        """Render a consolidation entry as markdown suitable for daily pages."""
        text = entry.rstrip()
        if not text:
            return ""
        lines = text.splitlines()
        first = f"- {lines[0]}"
        if len(lines) == 1:
            return first
        tail = "\n".join(f"  {line}" if line else "  " for line in lines[1:])
        return f"{first}\n{tail}"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def _append_daily_page(self, entry: str) -> None:
        """Append a consolidation entry to the current daily page."""
        daily_page = self._daily_page_path()
        daily_page.parent.mkdir(parents=True, exist_ok=True)
        if not daily_page.exists() or daily_page.stat().st_size == 0:
            daily_page.write_text(f"# {self._today_key()}\n\n", encoding="utf-8")

        formatted = self._format_daily_entry(entry)
        if not formatted:
            return

        with open(daily_page, "a", encoding="utf-8") as f:
            f.write(formatted.rstrip() + "\n\n")

    def _append_legacy_history(self, entry: str) -> None:
        """Append a consolidation entry to the legacy HISTORY.md file."""
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def append_history(self, entry: str) -> None:
        """Append a consolidation entry to both daily pages and legacy HISTORY.md."""
        self._append_daily_page(entry)
        self._append_legacy_history(entry)

    def read_daily_page(self, day_key: str | None = None) -> str:
        """Read a daily page if it exists."""
        path = self._daily_page_path(day_key)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def list_daily_pages(self) -> list[Path]:
        """List known daily memory pages."""
        pages = [
            path for path in self.memory_dir.glob("????-??-??.md")
            if path.name not in {"MEMORY.md", "HISTORY.md"}
        ]
        return sorted(pages, key=lambda p: p.name, reverse=True)

    def read_history_log(self) -> str:
        """Read the newest available history source.

        Daily pages are preferred; HISTORY.md remains a compatibility fallback.
        """
        daily_pages = self.list_daily_pages()
        if daily_pages:
            return "\n\n".join(page.read_text(encoding="utf-8") for page in daily_pages)
        return self.history_file.read_text(encoding="utf-8") if self.history_file.exists() else ""

    @staticmethod
    def _render_content(content: Any) -> str:
        """Render persisted message content for history output."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if isinstance(text, str) and text:
                        parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(parts) if parts else json.dumps(content, ensure_ascii=False)
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False)

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            if hasattr(session, "get_consolidation_messages"):
                old_messages = session.get_consolidation_messages(archive_all=True)
            else:
                old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            if hasattr(session, "get_consolidation_messages"):
                old_messages = session.get_consolidation_messages(keep_count=keep_count)
            else:
                old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            content = m.get("content")
            rendered = self._render_content(content)
            if not rendered:
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {rendered}")

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            # Some providers return arguments as a list (handle edge case)
            if isinstance(args, list):
                if args and isinstance(args[0], dict):
                    args = args[0]
                else:
                    logger.warning("Memory consolidation: unexpected arguments as empty or non-dict list")
                    return False
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
                if old_messages:
                    last_active = old_messages[-1].get("timestamp") or session.updated_at.isoformat()
                    session.metadata["_last_summary"] = {
                        "text": entry,
                        "last_active": str(last_active),
                    }
            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    self.write_long_term(update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False

    async def compact_idle_session(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        keep_recent_messages: int = 8,
    ) -> str:
        """Compact an idle session and return the persisted summary text."""
        old_messages = session.get_consolidation_messages(keep_count=keep_recent_messages)
        if not old_messages:
            return "(nothing)"

        previous = session.metadata.get("_last_summary")
        previous_text = previous.get("text") if isinstance(previous, dict) else None
        ok = await self.consolidate(
            session,
            provider,
            model,
            archive_all=False,
            memory_window=max(2, keep_recent_messages * 2),
        )
        if not ok:
            return ""

        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict) and isinstance(meta.get("text"), str):
            return meta["text"]
        return previous_text if isinstance(previous_text, str) else "(nothing)"
