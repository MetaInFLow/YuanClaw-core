"""Auto-compact idle sessions to reduce replay cost."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from loguru import logger

from yuanclaw.session.manager import Session, SessionManager


class IdleSessionConsolidator(Protocol):
    """Protocol implemented by idle-session consolidation adapters."""

    async def compact_idle_session(self, key: str, keep_recent_messages: int) -> str:
        """Compact an idle session and return the summary text, or ``(nothing)``."""


class AutoCompact:
    """Schedule background compaction for sessions idle beyond a configured TTL."""

    _RECENT_SUFFIX_MESSAGES = 8

    def __init__(
        self,
        sessions: SessionManager,
        consolidator: IdleSessionConsolidator,
        session_ttl_minutes: int = 0,
    ) -> None:
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}

    def _is_expired(self, ts: datetime | str | None, now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                return False
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        return f"Previous conversation summary (last active {last_active.isoformat()}):\n{text}"

    def check_expired(
        self,
        schedule_background: Callable[[Awaitable[Any]], None],
        active_session_keys: Collection[str] = (),
    ) -> None:
        """Schedule archival for idle sessions, skipping active and in-flight sessions."""
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = str(info.get("key") or "")
            if not key or key in self._archiving or key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)
                schedule_background(self._archive(key))

    async def _archive(self, key: str) -> None:
        try:
            summary = await self.consolidator.compact_idle_session(
                key,
                self._RECENT_SUFFIX_MESSAGES,
            )
            if summary and summary != "(nothing)":
                session = self.sessions.get_or_create(key)
                meta = session.metadata.get("_last_summary")
                if isinstance(meta, dict):
                    text = meta.get("text")
                    last_active = meta.get("last_active")
                    if isinstance(text, str) and isinstance(last_active, str):
                        self._summaries[key] = (text, datetime.fromisoformat(last_active))
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        """Reload an expired/archiving session and return a pending summary if available."""
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)

        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])

        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            text = meta.get("text")
            last_active = meta.get("last_active")
            if isinstance(text, str) and isinstance(last_active, str):
                return session, self._format_summary(text, datetime.fromisoformat(last_active))
        return session, None
