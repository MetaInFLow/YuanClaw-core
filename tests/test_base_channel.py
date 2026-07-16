import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuanclaw.bus.events import OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.whatsapp import WhatsAppChannel
from yuanclaw.config.schema import WhatsAppConfig


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        return None


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


def test_is_allowed_supports_plugin_dict_config() -> None:
    snake_case = _DummyChannel({"allow_from": ["allowed"]}, MessageBus())
    camel_case = _DummyChannel({"allowFrom": ["allowed"]}, MessageBus())

    assert snake_case.is_allowed("allowed") is True
    assert camel_case.is_allowed("allowed") is True
    assert snake_case.is_allowed("blocked") is False


@pytest.mark.asyncio
async def test_whatsapp_send_reports_disconnected_bridge() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())

    with pytest.raises(ConnectionError, match="not connected"):
        await channel.send(OutboundMessage(channel="whatsapp", chat_id="1", content="hello"))


@pytest.mark.asyncio
async def test_whatsapp_send_failure_marks_connection_unhealthy() -> None:
    class BrokenSocket:
        async def send(self, _payload: str) -> None:
            raise RuntimeError("closed")

    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel._ws = BrokenSocket()
    channel._connected = True

    with pytest.raises(RuntimeError, match="closed"):
        await channel.send(OutboundMessage(channel="whatsapp", chat_id="1", content="hello"))

    assert channel._connected is False


@pytest.mark.asyncio
async def test_whatsapp_group_message_uses_participant_for_access_control() -> None:
    config = WhatsAppConfig(allow_from=["15550001111"])
    channel = WhatsAppChannel(config, MessageBus())
    channel._handle_message = AsyncMock()

    await channel._handle_bridge_message(json.dumps({
        "type": "message",
        "id": "group-message-1",
        "sender": "group@g.us",
        "participant": "member@lid",
        "participantPn": "15550001111@s.whatsapp.net",
        "content": "hello",
        "isGroup": True,
    }))

    channel._handle_message.assert_awaited_once()
    call = channel._handle_message.await_args.kwargs
    assert call["sender_id"] == "15550001111"
    assert call["chat_id"] == "group@g.us"
    assert call["metadata"]["participant"] == "member@lid"


@pytest.mark.asyncio
async def test_whatsapp_voice_media_is_transcribed_when_available(tmp_path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"voice")
    channel = WhatsAppChannel(WhatsAppConfig(allow_from=["*"]), MessageBus())
    channel.transcribe_audio = AsyncMock(return_value="transcribed voice")
    channel._handle_message = AsyncMock()

    await channel._handle_bridge_message(json.dumps({
        "type": "message",
        "id": "voice-message-1",
        "sender": "15550002222@s.whatsapp.net",
        "content": "[Voice Message]",
        "isGroup": False,
        "media": [str(audio)],
    }))

    channel.transcribe_audio.assert_awaited_once_with(str(audio))
    call = channel._handle_message.await_args.kwargs
    assert call["content"].startswith("transcribed voice")
    assert call["media"] == [str(audio)]
