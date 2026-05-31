"""Signal channel using signal-cli daemon JSON-RPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.config.schema import SignalConfig
from yuanclaw.pairing import format_pairing_reply, generate_code, is_approved

_INLINE_MARKDOWN_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|`([^`\n]+)`")


def _plain_signal_text(text: str) -> str:
    """Small markdown-to-plain subset for Signal messages."""

    def repl(match: re.Match[str]) -> str:
        return next(group for group in match.groups() if group is not None)

    return _INLINE_MARKDOWN_RE.sub(repl, text or "")


class SignalChannel(BaseChannel):
    name = "signal"
    display_name = "Signal"

    def __init__(self, config: SignalConfig | dict[str, Any], bus: MessageBus):
        if isinstance(config, dict):
            config = SignalConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SignalConfig = config
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self._receive_task: asyncio.Task | None = None

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return SignalConfig().model_dump(by_alias=True)

    async def start(self) -> None:
        self._running = True
        self._client = httpx.AsyncClient(
            base_url=f"http://{self.config.daemon_host}:{self.config.daemon_port}",
            timeout=60,
        )
        if self.config.health_check_enabled:
            await self.health_check()
        if self.config.receive_events:
            self._receive_task = asyncio.create_task(self._receive_loop())

    async def stop(self) -> None:
        self._running = False
        if self._receive_task is not None:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> None:
        """Verify the signal-cli daemon is reachable."""
        client = self._client
        close_after = False
        if client is None:
            client = httpx.AsyncClient(
                base_url=f"http://{self.config.daemon_host}:{self.config.daemon_port}",
                timeout=10,
            )
            close_after = True
        try:
            response = await client.get("/api/v1/check")
            if response.status_code >= 400:
                raise RuntimeError(f"Signal daemon health check failed: HTTP {response.status_code}")
        finally:
            if close_after:
                await client.aclose()

    async def _receive_loop(self) -> None:
        delay = self.config.reconnect_delay_s
        max_delay = max(delay, self.config.max_reconnect_delay_s)
        while self._running:
            try:
                await self.receive_events_once()
                delay = self.config.reconnect_delay_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("Signal receive loop error: {}", exc)
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2 if delay > 0 else max_delay)

    async def receive_events_once(self) -> int:
        """Consume one SSE stream from signal-cli and publish accepted envelopes."""
        client = self._client
        close_after = False
        if client is None:
            client = httpx.AsyncClient(
                base_url=f"http://{self.config.daemon_host}:{self.config.daemon_port}",
                timeout=None,
            )
            close_after = True
        handled = 0
        try:
            async with client.stream("GET", "/api/v1/events") as response:
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Signal receive loop ignored malformed SSE payload")
                        continue
                    if await self.handle_envelope(payload):
                        handled += 1
        finally:
            if close_after:
                await client.aclose()
        return handled

    async def send(self, msg: OutboundMessage) -> None:
        params: dict[str, Any] = {
            "recipient": msg.chat_id,
            "message": _plain_signal_text(msg.content),
        }
        if msg.media:
            params["attachments"] = list(msg.media)
        response = await self._rpc("send", params)
        if "error" in response:
            raise RuntimeError(f"signal-cli send failed: {response['error']}")

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        close_after = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                base_url=f"http://{self.config.daemon_host}:{self.config.daemon_port}",
                timeout=60,
            )
            close_after = True
        try:
            response = await client.post("/api/v1/rpc", json=payload)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        finally:
            if close_after:
                await client.aclose()

    async def handle_envelope(self, payload: dict[str, Any]) -> bool:
        """Parse one signal-cli envelope and publish an inbound message if allowed."""
        envelope = payload.get("envelope") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict):
            return False
        data = envelope.get("dataMessage")
        if not isinstance(data, dict):
            return False

        source_number = str(envelope.get("sourceNumber") or "").strip()
        source_uuid = str(envelope.get("sourceUuid") or envelope.get("sourceUuidString") or "").strip()
        sender_id = "|".join(part for part in (source_number, source_uuid) if part)
        if not sender_id:
            return False

        group_info = data.get("groupInfo")
        is_group = isinstance(group_info, dict) and bool(group_info.get("groupId"))
        chat_id = str(group_info.get("groupId") if is_group else source_number).strip()
        if not chat_id:
            return False

        text = str(data.get("message") or "").strip()
        media = self._attachment_paths(data)
        if not text and not media:
            return False

        if not self._allowed(sender_id, chat_id, is_group):
            if not is_group:
                await self._send_pairing_reply(source_number, sender_id)
            return False

        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                media=media,
                metadata={
                    "signal": {
                        "is_dm": not is_group,
                        "is_group": is_group,
                        "timestamp": data.get("timestamp"),
                    }
                },
                session_key_override=f"signal:{chat_id}",
            )
        )
        return True

    async def _send_pairing_reply(self, source_number: str, sender_id: str) -> None:
        if not self.config.dm.pairing_reply_enabled:
            return
        target = source_number or sender_id.split("|", 1)[0]
        if not target:
            return
        pairing_subject = source_number or sender_id
        code = generate_code(self.name, pairing_subject, ttl=self.config.dm.pairing_ttl_s)
        await self.send(
            OutboundMessage(
                channel=self.name,
                chat_id=target,
                content=format_pairing_reply(code),
            )
        )

    def _attachment_paths(self, data: dict[str, Any]) -> list[str]:
        root = Path(self.config.attachments_dir).expanduser() if self.config.attachments_dir else None
        if root is None:
            return []
        paths: list[str] = []
        attachments = data.get("attachments")
        if not isinstance(attachments, list):
            return []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or item.get("id") or "").strip()
            if not filename:
                continue
            candidate = (root / filename).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                paths.append(str(candidate))
        return paths

    def _allowed(self, sender_id: str, chat_id: str, is_group: bool) -> bool:
        if is_group:
            cfg = self.config.group
            if not cfg.enabled:
                return False
            if cfg.policy == "open" or "*" in cfg.allow_from:
                return True
            return chat_id in cfg.allow_from

        cfg = self.config.dm
        if not cfg.enabled:
            return False
        if cfg.policy == "open" or "*" in cfg.allow_from:
            return True
        if self._sender_matches(sender_id, cfg.allow_from):
            return True
        return any(is_approved(self.name, part) for part in sender_id.split("|"))

    @staticmethod
    def _sender_matches(sender_id: str, allow_from: list[str]) -> bool:
        parts = set(sender_id.split("|"))
        return any(item in parts for item in allow_from)
