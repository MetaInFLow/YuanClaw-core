from pathlib import Path
from types import SimpleNamespace

import pytest

from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.discord import DiscordChannel
from yuanclaw.channels.feishu import FeishuChannel
from yuanclaw.config.schema import FeishuConfig


def test_discord_attachment_filename_removes_path_separators() -> None:
    filename = DiscordChannel._attachment_filename(
        {"id": "item/1", "filename": "../folder\\report.pdf"}
    )

    assert "/" not in filename
    assert "\\" not in filename
    assert filename.endswith("report.pdf")


@pytest.mark.asyncio
async def test_feishu_rejects_sender_before_reaction_or_media_processing() -> None:
    channel = FeishuChannel(FeishuConfig(allow_from=["allowed"]), MessageBus())
    channel._add_reaction = _unexpected_async_call
    channel._download_and_save_media = _unexpected_async_call
    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="msg-1",
                chat_id="chat-1",
                chat_type="p2p",
                message_type="file",
                content='{"file_key":"file-1"}',
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="blocked"),
            ),
        )
    )

    await channel._on_message(data)


@pytest.mark.asyncio
async def test_feishu_media_uses_safe_unique_filename(monkeypatch, tmp_path: Path) -> None:
    channel = FeishuChannel(FeishuConfig(allow_from=["*"]), MessageBus())
    monkeypatch.setattr("yuanclaw.channels.feishu.get_media_dir", lambda _name: tmp_path)
    monkeypatch.setattr(
        channel,
        "_download_file_sync",
        lambda *_args: (b"content", "../folder\\report.pdf"),
    )

    path, marker = await channel._download_and_save_media(
        "file", {"file_key": "file-1"}, "message-1"
    )

    assert path is not None
    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("message-1_")
    assert Path(path).read_bytes() == b"content"
    assert marker == "[file: report.pdf]"


@pytest.mark.asyncio
async def test_feishu_media_rejects_oversized_content(monkeypatch, tmp_path: Path) -> None:
    channel = FeishuChannel(FeishuConfig(allow_from=["*"]), MessageBus())
    monkeypatch.setattr("yuanclaw.channels.feishu.get_media_dir", lambda _name: tmp_path)
    monkeypatch.setattr("yuanclaw.channels.feishu.MAX_ATTACHMENT_BYTES", 8)
    monkeypatch.setattr(
        channel,
        "_download_file_sync",
        lambda *_args: (b"123456789", "large.bin"),
    )

    path, marker = await channel._download_and_save_media(
        "file", {"file_key": "file-1"}, "message-1"
    )

    assert path is None
    assert marker == "[file: large.bin - too large]"
    assert list(tmp_path.iterdir()) == []


async def _unexpected_async_call(*_args, **_kwargs):
    raise AssertionError("unauthorized message performed side effects")
