"""Auto-compact idle session behavior."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuanclaw.agent.autocompact import AutoCompact
from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.memory import MemoryStore
from yuanclaw.bus.events import InboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.config.schema import CompactionConfig
from yuanclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from yuanclaw.session.manager import Session, SessionManager


def _make_session(
    key: str = "cli:test",
    *,
    updated_at: datetime | None = None,
    metadata: dict | None = None,
) -> Session:
    session = Session(key=key, metadata=metadata or {})
    if updated_at is not None:
        session.updated_at = updated_at
    return session


def _make_autocompact(
    ttl: int = 15,
    sessions: SessionManager | None = None,
    consolidator: MagicMock | None = None,
) -> AutoCompact:
    if sessions is None:
        sessions = MagicMock(spec=SessionManager)
    if consolidator is None:
        consolidator = MagicMock()
        consolidator.compact_idle_session = AsyncMock(return_value="Summary.")
    return AutoCompact(sessions, consolidator, session_ttl_minutes=ttl)


def test_is_expired_respects_disabled_ttl() -> None:
    ac = _make_autocompact(ttl=0)
    assert ac._is_expired(datetime.now() - timedelta(days=1)) is False


def test_is_expired_accepts_iso_strings() -> None:
    ac = _make_autocompact(ttl=15)
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert ac._is_expired((now - timedelta(minutes=15)).isoformat(), now=now) is True
    assert ac._is_expired((now - timedelta(minutes=14, seconds=59)).isoformat(), now=now) is False


def test_check_expired_schedules_idle_sessions_only() -> None:
    sessions = MagicMock(spec=SessionManager)
    sessions.list_sessions.return_value = [
        {"key": "cli:old", "updated_at": (datetime.now() - timedelta(minutes=20)).isoformat()},
        {"key": "cli:busy", "updated_at": (datetime.now() - timedelta(minutes=20)).isoformat()},
        {"key": "cli:fresh", "updated_at": datetime.now().isoformat()},
        {"key": "", "updated_at": (datetime.now() - timedelta(minutes=20)).isoformat()},
    ]
    scheduled = []

    def scheduler(awaitable):
        scheduled.append(awaitable)
        awaitable.close()

    ac = _make_autocompact(ttl=15, sessions=sessions)
    ac.check_expired(scheduler, active_session_keys={"cli:busy"})

    assert len(scheduled) == 1
    assert "cli:old" in ac._archiving
    assert "cli:busy" not in ac._archiving
    assert "cli:fresh" not in ac._archiving


@pytest.mark.asyncio
async def test_archive_delegates_and_caches_metadata_summary() -> None:
    sessions = MagicMock(spec=SessionManager)
    sessions.get_or_create.return_value = _make_session(
        metadata={
            "_last_summary": {
                "text": "Persisted summary.",
                "last_active": "2026-05-13T10:00:00",
            }
        }
    )
    consolidator = MagicMock()
    consolidator.compact_idle_session = AsyncMock(return_value="Persisted summary.")
    ac = _make_autocompact(sessions=sessions, consolidator=consolidator)
    ac._archiving.add("cli:test")

    await ac._archive("cli:test")

    consolidator.compact_idle_session.assert_awaited_once_with("cli:test", ac._RECENT_SUFFIX_MESSAGES)
    assert ac._summaries["cli:test"][0] == "Persisted summary."
    assert "cli:test" not in ac._archiving


@pytest.mark.asyncio
async def test_archive_clears_archiving_on_failure() -> None:
    consolidator = MagicMock()
    consolidator.compact_idle_session = AsyncMock(side_effect=RuntimeError("boom"))
    ac = _make_autocompact(consolidator=consolidator)
    ac._archiving.add("cli:test")

    await ac._archive("cli:test")

    assert "cli:test" not in ac._archiving


def test_prepare_session_reloads_when_archiving_or_expired() -> None:
    sessions = MagicMock(spec=SessionManager)
    reloaded = _make_session()
    sessions.get_or_create.return_value = reloaded
    ac = _make_autocompact(ttl=15, sessions=sessions)
    ac._archiving.add("cli:test")

    session, summary = ac.prepare_session(_make_session(), "cli:test")
    assert session is reloaded
    assert summary is None

    ac._archiving.clear()
    old = _make_session(updated_at=datetime.now() - timedelta(minutes=20))
    session, _ = ac.prepare_session(old, "cli:test")
    assert session is reloaded
    assert sessions.get_or_create.call_count == 2


def test_prepare_session_returns_hot_summary_once() -> None:
    ac = _make_autocompact()
    ac._summaries["cli:test"] = ("Hot summary.", datetime(2026, 1, 1))

    _, summary = ac.prepare_session(_make_session(), "cli:test")
    assert summary is not None
    assert "Hot summary." in summary
    assert "Previous conversation summary" in summary

    _, summary = ac.prepare_session(_make_session(), "cli:test")
    assert summary is None


def test_prepare_session_returns_cold_summary_from_metadata() -> None:
    ac = _make_autocompact()
    session = _make_session(
        metadata={
            "_last_summary": {
                "text": "Cold summary.",
                "last_active": datetime(2026, 1, 1).isoformat(),
            }
        }
    )

    _, summary = ac.prepare_session(session, "cli:test")
    assert summary is not None
    assert "Cold summary." in summary


class _MemoryProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="save1",
                    name="save_memory",
                    arguments={
                        "history_entry": "Compacted summary.",
                        "memory_update": "Long-term memory.",
                    },
                )
            ],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_memory_store_compact_idle_session_persists_summary(tmp_path) -> None:
    session = _make_session()
    for index in range(6):
        session.add_message("user", f"user {index}", timestamp=f"2026-01-01T10:0{index}:00")
        session.add_message("assistant", f"assistant {index}", timestamp=f"2026-01-01T10:0{index}:30")

    summary = await MemoryStore(tmp_path).compact_idle_session(
        session,
        _MemoryProvider(),
        "test-model",
        keep_recent_messages=4,
    )

    assert summary == "Compacted summary."
    assert session.last_consolidated == 8
    assert session.metadata["_last_summary"]["text"] == "Compacted summary."
    assert session.metadata["_last_summary"]["last_active"] == "2026-01-01T10:03:30"


@pytest.mark.asyncio
async def test_agent_loop_injects_pending_auto_compact_summary(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_MemoryProvider(),
        workspace=tmp_path,
        compaction_config=CompactionConfig(session_ttl_minutes=15),
    )
    session = loop.sessions.get_or_create("cli:direct")
    session.metadata["_last_summary"] = {
        "text": "Cold compacted history.",
        "last_active": "2026-01-01T10:00:00",
    }

    captured: dict[str, list[dict]] = {}

    async def fake_run(messages, **kwargs):
        captured["messages"] = messages
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    result = await loop._process_message(
        InboundMessage(channel="cli", chat_id="direct", sender_id="u", content="continue")
    )

    assert result is not None
    assert result.content == "done"
    user_message = captured["messages"][-1]["content"]
    assert "Previous conversation summary" in user_message
    assert "Cold compacted history." in user_message
