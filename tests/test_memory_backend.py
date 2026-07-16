import sqlite3
from pathlib import Path

import pytest

from yuanclaw.memory import CoreMemoryBackend, LegacyMemoryBackend, MemoryHit


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_core_memory_backend_search_and_get(tmp_path: Path) -> None:
    _write(
        tmp_path / "MEMORY.md",
        "# MEMORY\n\nalpha durable fact\n",
    )
    _write(
        tmp_path / "memory" / "2026-03-23.md",
        "# 2026-03-23\n\nbeta daily note\n",
    )
    _write(
        tmp_path / "memory" / "HISTORY.md",
        "# HISTORY\n\nlegacy note\n",
    )

    backend = CoreMemoryBackend(tmp_path)

    hits = backend.search("alpha", max_results=5)
    assert hits
    assert isinstance(hits[0], MemoryHit)
    assert hits[0].path == "MEMORY.md"
    assert hits[0].line == 3
    assert hits[0].citation == "MEMORY.md:3"

    daily_hits = backend.search("beta", max_results=5)
    assert daily_hits
    assert daily_hits[0].path == "memory/2026-03-23.md"
    assert daily_hits[0].citation == "memory/2026-03-23.md:3"

    doc = backend.get("memory/2026-03-23.md", start_line=2, end_line=3)
    assert doc.path == "memory/2026-03-23.md"
    assert doc.citation == "memory/2026-03-23.md:2-3"
    assert doc.render_with_lines() == "2 | \n3 | beta daily note"


def test_core_memory_backend_build_context_respects_group_flag(tmp_path: Path) -> None:
    _write(
        tmp_path / "MEMORY.md",
        "# MEMORY\n\nalpha durable fact\n",
    )
    _write(
        tmp_path / "memory" / "2026-03-23.md",
        "# 2026-03-23\n\nbeta daily note\n",
    )

    backend = CoreMemoryBackend(tmp_path)

    private_context = backend.build_context("telegram:chat-1", "telegram", "chat-1")
    assert "Long-term Memory" in private_context
    assert "alpha durable fact" in private_context
    assert "Recent Memory" in private_context
    assert "beta daily note" in private_context

    group_context = backend.build_context("session-1", "telegram", "chat-1", is_group=True)
    assert group_context == ""


def test_core_memory_backend_non_main_session_skips_recent_pages(tmp_path: Path) -> None:
    _write(
        tmp_path / "MEMORY.md",
        "# MEMORY\n\nalpha durable fact\n",
    )
    _write(
        tmp_path / "memory" / "2026-03-23.md",
        "# 2026-03-23\n\nbeta daily note\n",
    )

    backend = CoreMemoryBackend(tmp_path, recent_days=2)

    context = backend.build_context("telegram:chat-1:thread-1", "telegram", "chat-1")

    assert "Long-term Memory" in context
    assert "alpha durable fact" in context
    assert "Recent Memory" not in context
    assert "beta daily note" not in context


def test_core_memory_backend_indexes_extra_paths(tmp_path: Path) -> None:
    _write(tmp_path / "notes" / "project.md", "# Notes\n\nexternal context fact\n")

    backend = CoreMemoryBackend(tmp_path, extra_paths=["notes"])

    hits = backend.search("external context", max_results=5)

    assert hits
    assert hits[0].path == "notes/project.md"


def test_legacy_memory_backend_only_uses_legacy_memory_dir(tmp_path: Path) -> None:
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\nroot fact\n")
    _write(tmp_path / "memory" / "MEMORY.md", "# MEMORY\n\nlegacy fact\n")
    _write(tmp_path / "memory" / "HISTORY.md", "# HISTORY\n\nlegacy history\n")

    backend = LegacyMemoryBackend(tmp_path)

    indexed = [path.relative_to(tmp_path).as_posix() for path in backend._indexed_paths()]
    assert indexed == ["memory/MEMORY.md", "memory/HISTORY.md"]

    hits = backend.search("root", max_results=5)
    assert hits == []

    context = backend.build_context("session-1", "telegram", "chat-1")
    assert "legacy fact" in context
    assert "root fact" not in context


def test_memory_index_records_size_and_content_digest(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write(memory_file, "# MEMORY\n\nalpha fact\n")
    backend = CoreMemoryBackend(tmp_path)

    assert backend.search("alpha")

    with sqlite3.connect(backend._index.db_path) as conn:
        size_bytes, digest = conn.execute(
            "SELECT size_bytes, content_sha256 FROM indexed_files WHERE path = 'MEMORY.md'"
        ).fetchone()
    assert size_bytes == memory_file.stat().st_size
    assert len(digest) == 64


def test_memory_index_preserves_corrupt_database_and_rebuilds(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    index_path = memory_dir / ".memory-index.sqlite3"
    index_path.write_bytes(b"not a sqlite database")
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\nrecoverable fact\n")

    backend = CoreMemoryBackend(tmp_path)

    assert backend.search("recoverable")
    assert index_path.is_file()
    assert list(memory_dir.glob(".memory-index.sqlite3.corrupt-*"))


def test_memory_get_does_not_refresh_search_index(tmp_path: Path) -> None:
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\ndirect read\n")
    backend = CoreMemoryBackend(tmp_path)
    backend._refresh_index_if_needed = lambda: (_ for _ in ()).throw(
        AssertionError("direct reads must not refresh the index")
    )

    doc = backend.get("MEMORY.md")

    assert "direct read" in doc.content


def test_memory_get_rejects_existing_file_outside_inventory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not memory", encoding="utf-8")
    backend = CoreMemoryBackend(tmp_path)

    with pytest.raises(PermissionError, match="outside the configured inventory"):
        backend.get(str(outside))


def test_memory_get_allows_explicit_extra_path(tmp_path: Path) -> None:
    extra = tmp_path / "notes" / "project.md"
    _write(extra, "# Project\n\nallowed context\n")
    backend = CoreMemoryBackend(tmp_path, extra_paths=["notes"])

    doc = backend.get("notes/project.md")

    assert "allowed context" in doc.content
