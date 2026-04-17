from __future__ import annotations

import pytest

from yuanclaw.agent.tools.filesystem import ReadFileTool


@pytest.mark.asyncio
async def test_read_file_returns_image_content_blocks(tmp_path) -> None:
    path = tmp_path / "sample.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo-bytes")

    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute("sample.png")

    assert isinstance(result, list)
    assert result[0]["type"] == "image_url"
    assert result[0]["_meta"]["path"].endswith("sample.png")
    assert result[1]["text"] == "(Image file: sample.png)"
