from __future__ import annotations

import json

import httpx
import pytest

from yuanclaw.bus.events import OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.manager import ChannelManager
from yuanclaw.channels.signal import SignalChannel
from yuanclaw.config.schema import Config, SignalConfig
from yuanclaw.pairing import approve_code, generate_code, is_approved


def test_signal_config_accepts_nested_dm_and_group_policy() -> None:
    config = SignalConfig.model_validate(
        {
            "enabled": True,
            "phoneNumber": "+15550001111",
            "daemonHost": "127.0.0.1",
            "daemonPort": 18080,
            "attachmentsDir": "/tmp/signal-attachments",
            "dm": {"enabled": True, "policy": "allowlist", "allowFrom": ["+15550002222"]},
            "group": {
                "enabled": True,
                "policy": "open",
                "allowFrom": ["group-1"],
                "requireMention": False,
            },
        }
    )

    assert config.phone_number == "+15550001111"
    assert config.attachments_dir == "/tmp/signal-attachments"
    assert config.allow_from == ["+15550002222", "group-1"]


def test_channel_manager_discovers_signal_when_enabled() -> None:
    config = Config()
    config.channels.signal.enabled = True
    config.channels.signal.phone_number = "+15550001111"
    config.channels.signal.dm.allow_from = ["*"]

    manager = ChannelManager(config, MessageBus())

    assert isinstance(manager.get_channel("signal"), SignalChannel)


def test_pairing_store_approves_signal_sender(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.config.paths.get_config_path", lambda: tmp_path / "config.json")

    code = generate_code("signal", "+15550002222", ttl=60)

    assert is_approved("signal", "+15550002222") is False
    assert approve_code(code) == ("signal", "+15550002222")
    assert is_approved("signal", "+15550002222") is True


@pytest.mark.asyncio
async def test_signal_dm_allowlist_publishes_inbound_message(tmp_path) -> None:
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            dm={"enabled": True, "policy": "allowlist", "allow_from": ["+15550002222"]},
            attachments_dir=str(tmp_path),
        ),
        bus,
    )
    attachment = tmp_path / "photo.jpg"
    attachment.write_bytes(b"jpeg")

    accepted = await channel.handle_envelope(
        {
            "envelope": {
                "sourceNumber": "+15550002222",
                "sourceUuid": "uuid-1",
                "dataMessage": {
                    "message": "hello",
                    "timestamp": 123,
                    "attachments": [{"filename": "photo.jpg"}],
                },
            }
        }
    )

    assert accepted is True
    inbound = await bus.consume_inbound()
    assert inbound.channel == "signal"
    assert inbound.sender_id == "+15550002222|uuid-1"
    assert inbound.chat_id == "+15550002222"
    assert inbound.content == "hello"
    assert inbound.media == [str(attachment)]
    assert inbound.metadata["signal"]["is_dm"] is True


@pytest.mark.asyncio
async def test_signal_dm_denied_when_not_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.config.paths.get_config_path", lambda: tmp_path / "config.json")
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            dm={"enabled": True, "policy": "allowlist", "allow_from": ["+15550003333"]},
        ),
        bus,
    )

    accepted = await channel.handle_envelope(
        {
            "envelope": {
                "sourceNumber": "+15550002222",
                "dataMessage": {"message": "hello"},
            }
        }
    )

    assert accepted is False
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_signal_denied_dm_can_send_pairing_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("yuanclaw.config.paths.get_config_path", lambda: tmp_path / "config.json")
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            dm={
                "enabled": True,
                "policy": "allowlist",
                "allow_from": ["+15550003333"],
                "pairing_reply_enabled": True,
            },
        ),
        bus,
    )
    sent: list[OutboundMessage] = []
    monkeypatch.setattr("yuanclaw.channels.signal.generate_code", lambda *args, **kwargs: "ABCD-1234")

    async def fake_send(msg: OutboundMessage) -> None:
        sent.append(msg)

    channel.send = fake_send

    accepted = await channel.handle_envelope(
        {
            "envelope": {
                "sourceNumber": "+15550002222",
                "sourceUuid": "uuid-1",
                "dataMessage": {"message": "please pair"},
            }
        }
    )

    assert accepted is False
    assert bus.inbound_size == 0
    assert len(sent) == 1
    assert sent[0].chat_id == "+15550002222"
    assert "ABCD-1234" in sent[0].content


@pytest.mark.asyncio
async def test_signal_group_policy_publishes_allowed_group(tmp_path) -> None:
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            group={"enabled": True, "policy": "allowlist", "allow_from": ["group-1"]},
        ),
        bus,
    )

    accepted = await channel.handle_envelope(
        {
            "envelope": {
                "sourceNumber": "+15550002222",
                "dataMessage": {
                    "message": "group hello",
                    "groupInfo": {"groupId": "group-1"},
                },
            }
        }
    )

    assert accepted is True
    inbound = await bus.consume_inbound()
    assert inbound.chat_id == "group-1"
    assert inbound.metadata["signal"]["is_group"] is True


@pytest.mark.asyncio
async def test_signal_attachment_paths_cannot_escape_attachment_dir(tmp_path) -> None:
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            dm={"enabled": True, "policy": "open"},
            attachments_dir=str(tmp_path),
        ),
        bus,
    )
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    accepted = await channel.handle_envelope(
        {
            "envelope": {
                "sourceNumber": "+15550002222",
                "dataMessage": {
                    "message": "see file",
                    "attachments": [{"filename": "../outside.txt"}],
                },
            }
        }
    )

    assert accepted is True
    inbound = await bus.consume_inbound()
    assert inbound.media == []


@pytest.mark.asyncio
async def test_signal_send_uses_json_rpc_with_attachments(tmp_path) -> None:
    channel = SignalChannel(
        SignalConfig(enabled=True, phone_number="+15550001111", dm={"allow_from": ["*"]}),
        MessageBus(),
    )
    calls = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        return {"result": {"timestamp": 1}}

    channel._rpc = fake_rpc

    await channel.send(
        OutboundMessage(
            channel="signal",
            chat_id="+15550002222",
            content="**hello**",
            media=[str(tmp_path / "a.png")],
        )
    )

    assert calls[0][0] == "send"
    assert calls[0][1]["recipient"] == "+15550002222"
    assert calls[0][1]["message"] == "hello"
    assert calls[0][1]["attachments"] == [str(tmp_path / "a.png")]


class _FakeSignalStream:
    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for chunk in self._chunks:
            for line in chunk.splitlines():
                yield line


@pytest.mark.asyncio
async def test_signal_health_check_raises_when_daemon_unhealthy() -> None:
    channel = SignalChannel(SignalConfig(enabled=True), MessageBus())

    async def fake_get(path):
        return httpx.Response(503, request=httpx.Request("GET", "http://signal.test" + path))

    class FakeClient:
        get = staticmethod(fake_get)

    channel._client = FakeClient()

    with pytest.raises(RuntimeError, match="health check failed"):
        await channel.health_check()


@pytest.mark.asyncio
async def test_signal_receive_events_publishes_sse_envelope(tmp_path) -> None:
    bus = MessageBus()
    channel = SignalChannel(
        SignalConfig(
            enabled=True,
            phone_number="+15550001111",
            dm={"enabled": True, "policy": "open"},
            attachments_dir=str(tmp_path),
        ),
        bus,
    )
    payload = {
        "envelope": {
            "sourceNumber": "+15550002222",
            "dataMessage": {"message": "hello over sse"},
        }
    }
    channel._client = type(
        "FakeClient",
        (),
        {"stream": lambda self, method, path: _FakeSignalStream([f"data: {json.dumps(payload)}\n\n"])},
    )()

    handled = await channel.receive_events_once()

    assert handled == 1
    inbound = await bus.consume_inbound()
    assert inbound.channel == "signal"
    assert inbound.content == "hello over sse"


@pytest.mark.asyncio
async def test_signal_receive_loop_reconnects_after_transient_error(monkeypatch) -> None:
    channel = SignalChannel(SignalConfig(enabled=True, dm={"enabled": True, "policy": "open"}), MessageBus())
    attempts = 0
    sleeps: list[float] = []

    async def fake_receive_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.TransportError("temporary")
        channel._running = False
        return 0

    async def fake_sleep(delay):
        sleeps.append(delay)

    channel.receive_events_once = fake_receive_once
    monkeypatch.setattr("yuanclaw.channels.signal.asyncio.sleep", fake_sleep)
    channel._running = True

    await channel._receive_loop()

    assert attempts == 2
    assert sleeps == [channel.config.reconnect_delay_s]
