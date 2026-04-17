from pathlib import Path

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
