from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import pytest

from yuanclaw.agent.memory import MemoryStore
from yuanclaw.config.loader import ConfigLoadError, load_config, save_config
from yuanclaw.config.schema import Config
from yuanclaw.providers.base import LLMResponse, ToolCallRequest
from yuanclaw.session.manager import Session, SessionCorruptError, SessionManager
from yuanclaw.utils import atomic


class _MemoryProvider:
    def __init__(self, *, started: asyncio.Event | None = None, release: asyncio.Event | None = None):
        self.started = started
        self.release = release

    async def chat(self, messages, **_kwargs) -> LLMResponse:
        prompt = messages[-1]["content"]
        conversation = prompt.split("## Conversation to Process\n", 1)[1]
        fact = "alpha" if "alpha" in conversation else "beta"
        current = prompt.split("## Current Long-term Memory\n", 1)[1].split(
            "\n\n## Conversation to Process",
            1,
        )[0]
        if current == "(empty)":
            current = ""
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        await asyncio.sleep(0.01)
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id=f"save-{fact}",
                    name="save_memory",
                    arguments={
                        "history_entry": f"[{fact}] archived",
                        "memory_update": (current.rstrip() + f"\n{fact}").strip(),
                    },
                )
            ],
        )


def _config(workspace: str) -> Config:
    config = Config()
    config.agents.defaults.workspace = workspace
    return config


def test_config_atomic_replace_failure_preserves_previous_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    save_config(_config("first"), path)
    original_replace = atomic.os.replace

    def fail_target_replace(source, destination):
        if destination == path:
            raise OSError("simulated replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(_config("second"), path)

    assert load_config(path).agents.defaults.workspace == "first"


def test_config_recovers_valid_backup_and_quarantines_damage(tmp_path) -> None:
    path = tmp_path / "config.json"
    save_config(_config("first"), path)
    save_config(_config("second"), path)
    path.write_text("{broken", encoding="utf-8")

    recovered = load_config(path)

    assert recovered.agents.defaults.workspace == "first"
    assert load_config(path).agents.defaults.workspace == "first"
    assert list(tmp_path.glob("config.json.corrupt-*"))


def test_config_damage_without_valid_backup_is_explicit(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigLoadError, match="Invalid configuration file"):
        load_config(path)

    assert path.exists()


def test_session_hash_prevents_legacy_filename_collision(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    first = Session(key="a:b_c")
    first.add_message("user", "first")
    second = Session(key="a_b:c")
    second.add_message("user", "second")

    manager.save(first)
    manager.save(second)

    first_path = manager._get_session_path(first.key)
    second_path = manager._get_session_path(second.key)
    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()

    reloaded = SessionManager(tmp_path)
    assert reloaded.get_or_create(first.key).messages[0]["content"] == "first"
    assert reloaded.get_or_create(second.key).messages[0]["content"] == "second"


def test_session_atomic_replace_failure_preserves_previous_turn(tmp_path, monkeypatch) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="studio:thread")
    session.add_message("user", "first")
    manager.save(session)
    path = manager._get_session_path(session.key)
    original_replace = atomic.os.replace

    def fail_target_replace(source, destination):
        if destination == path:
            raise OSError("simulated replace failure")
        return original_replace(source, destination)

    session.add_message("assistant", "second")
    monkeypatch.setattr(atomic.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        manager.save(session)

    loaded = SessionManager(tmp_path).get_or_create(session.key)
    assert [message["content"] for message in loaded.messages] == ["first"]


def test_session_recovers_backup_and_restores_timestamps(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="studio:thread")
    session.created_at = datetime.now() - timedelta(days=2)
    session.updated_at = datetime.now() - timedelta(days=1)
    session.add_message("user", "first")
    expected_updated_at = session.updated_at
    manager.save(session)
    session.add_message("assistant", "second")
    manager.save(session)
    path = manager._get_session_path(session.key)
    path.write_text("{broken", encoding="utf-8")

    recovered = SessionManager(tmp_path).get_or_create(session.key)

    assert [message["content"] for message in recovered.messages] == ["first"]
    assert recovered.updated_at == expected_updated_at
    assert list(path.parent.glob(f"{path.name}.corrupt-*"))


def test_session_damage_without_backup_does_not_become_empty_session(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    path = manager._get_session_path("studio:thread")
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(SessionCorruptError, match="Invalid session file"):
        manager.get_or_create("studio:thread")

    assert path.exists()


def test_session_reload_clamps_invalid_consolidation_offset(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="studio:thread")
    session.add_message("user", "only")
    session.last_consolidated = 99
    manager.save(session)

    loaded = SessionManager(tmp_path).get_or_create(session.key)

    assert loaded.last_consolidated == 1


def test_session_cache_reloads_external_disk_update(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("studio:thread")
    session.add_message("user", "first")
    manager.save(session)
    path = manager._get_session_path(session.key)
    original_stat = path.stat()

    external = SessionManager(tmp_path)
    changed = external.get_or_create(session.key)
    changed.add_message("assistant", "external")
    external.save(changed)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    reloaded = manager.get_or_create(session.key)
    assert [message["content"] for message in reloaded.messages] == ["first", "external"]


def test_session_cache_does_not_revive_externally_deleted_file(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("studio:thread")
    session.add_message("user", "old")
    manager.save(session)
    manager._get_session_path(session.key).unlink()

    replacement = manager.get_or_create(session.key)

    assert replacement is not session
    assert replacement.messages == []


def test_session_delete_evicts_cache_and_removes_backup(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("studio:thread")
    session.add_message("user", "first")
    manager.save(session)
    session.add_message("assistant", "second")
    manager.save(session)
    path = manager._get_session_path(session.key)
    previous = atomic.backup_path(path)
    assert path.exists()
    assert previous.exists()

    assert manager.delete(session.key) is True

    assert not path.exists()
    assert not previous.exists()
    assert manager.get_or_create(session.key).messages == []


@pytest.mark.asyncio
async def test_memory_consolidation_serializes_workspace_updates(tmp_path) -> None:
    first = Session(key="studio:first")
    first.add_message("user", "alpha")
    first.add_message("assistant", "done")
    second = Session(key="studio:second")
    second.add_message("user", "beta")
    second.add_message("assistant", "done")
    provider = _MemoryProvider()

    results = await asyncio.gather(
        MemoryStore(tmp_path).consolidate(first, provider, "test", memory_window=2),
        MemoryStore(tmp_path).consolidate(second, provider, "test", memory_window=2),
    )

    assert results == [True, True]
    memory = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "alpha" in memory
    assert "beta" in memory


@pytest.mark.asyncio
async def test_memory_consolidation_offset_stops_at_snapshot_boundary(tmp_path) -> None:
    session = Session(key="studio:thread")
    for index in range(4):
        session.add_message("user", f"alpha-{index}")
    started = asyncio.Event()
    release = asyncio.Event()
    provider = _MemoryProvider(started=started, release=release)

    task = asyncio.create_task(
        MemoryStore(tmp_path).consolidate(session, provider, "test", memory_window=2)
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    session.add_message("user", "late-message")
    release.set()

    assert await task is True
    assert session.last_consolidated == 3
    assert len(session.messages) == 5


@pytest.mark.asyncio
async def test_memory_replace_failure_preserves_old_memory_and_deduplicates_retry(
    tmp_path,
    monkeypatch,
) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("original")
    session = Session(key="studio:thread")
    session.add_message("user", "alpha")
    session.add_message("assistant", "done")
    provider = _MemoryProvider()
    original_replace = atomic.os.replace

    def fail_memory_replace(source, destination):
        if destination == store.memory_file:
            raise OSError("simulated memory replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_memory_replace)
    assert await store.consolidate(session, provider, "test", memory_window=2) is False
    assert store.read_long_term() == "original"

    monkeypatch.setattr(atomic.os, "replace", original_replace)
    assert await store.consolidate(session, provider, "test", memory_window=2) is True
    history = store.history_file.read_text(encoding="utf-8")
    assert history.count("[alpha] archived") == 1
