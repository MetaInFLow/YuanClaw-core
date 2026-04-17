from pathlib import Path

import pytest

from yuanclaw.agent.tools.memory import MemoryGetTool, MemorySearchTool


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_memory_search_tool_returns_cited_hits(tmp_path: Path) -> None:
    _write(
        tmp_path / "MEMORY.md",
        "# MEMORY\n\nalpha durable fact\n",
    )
    tool = MemorySearchTool(workspace=tmp_path)

    result = await tool.execute(query="alpha", max_results=5)

    assert tool.name == "memory_search"
    assert "Memory hits for: alpha" in result
    assert "MEMORY.md:3" in result
    assert "alpha durable fact" in result


@pytest.mark.asyncio
async def test_memory_get_tool_returns_numbered_slice(tmp_path: Path) -> None:
    _write(
        tmp_path / "memory" / "2026-03-23.md",
        "# 2026-03-23\n\nalpha\nbeta\n",
    )
    tool = MemoryGetTool(workspace=tmp_path)

    result = await tool.execute(path="memory/2026-03-23.md", start_line=3, end_line=4)

    assert tool.name == "memory_get"
    assert "memory/2026-03-23.md:3-4" in result
    assert "3 | alpha" in result
    assert "4 | beta" in result


@pytest.mark.asyncio
async def test_memory_get_tool_reports_errors(tmp_path: Path) -> None:
    tool = MemoryGetTool(workspace=tmp_path)

    result = await tool.execute(path="missing.md")

    assert result.startswith("Error reading memory:")
