"""Local HTTP/WebSocket API used by YuanClaw Studio."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from yuanclaw import __version__
from yuanclaw.agent.loop import AgentLoop
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.manager import ChannelManager
from yuanclaw.config.loader import save_config
from yuanclaw.config.paths import get_cron_dir
from yuanclaw.config.schema import Config
from yuanclaw.cron.service import CronService
from yuanclaw.cron.types import CronJob
from yuanclaw.providers.registry import PROVIDERS, find_by_name
from yuanclaw.session.manager import SessionManager
from yuanclaw.utils.helpers import sync_workspace_templates


def _make_provider(config: Config):
    """Create provider object from config (same behavior as CLI gateway path)."""
    from yuanclaw.providers.azure_openai_provider import AzureOpenAIProvider
    from yuanclaw.providers.custom_provider import CustomProvider
    from yuanclaw.providers.litellm_provider import LiteLLMProvider
    from yuanclaw.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    if provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            logger.warning("Azure OpenAI config incomplete, falling back to local custom provider")
            return CustomProvider(
                api_key="no-key",
                api_base="http://localhost:8000/v1",
                default_model=model,
            )
        return AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )

    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        logger.warning("No API key configured, chat responses may fail until provider is configured")
        return CustomProvider(
            api_key="no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )


def _mask_secret(value: str) -> str:
    """Mask secrets for UI display."""
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def _extract_description(skill_file: Path) -> str:
    """Extract skill description from frontmatter."""
    try:
        content = skill_file.read_text(encoding="utf-8")
    except Exception:
        return "No description"

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                line = line.strip()
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("'\"") or "No description"

    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:200]

    return "No description"


def _list_skills() -> list[dict[str, Any]]:
    """List built-in skills from package directory."""
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    if not skills_dir.exists():
        return []

    items: list[dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        items.append(
            {
                "id": entry.name,
                "name": entry.name,
                "description": _extract_description(skill_file),
                "version": "built-in",
                "enabled": True,
            }
        )
    return items


def _provider_rows(config: Config) -> list[dict[str, Any]]:
    """Summarize providers for Settings page."""
    selected = config.get_provider_name(config.agents.defaults.model)
    rows: list[dict[str, Any]] = []

    for spec in PROVIDERS:
        provider = getattr(config.providers, spec.name, None)
        if provider is None:
            continue

        configured = bool(spec.is_oauth or provider.api_key or provider.api_base)
        rows.append(
            {
                "id": spec.name,
                "name": spec.label,
                "configured": configured,
                "is_default": spec.name == selected,
                "api_key_masked": _mask_secret(provider.api_key or ""),
                "api_base": provider.api_base or "",
            }
        )

    return rows


def _channel_is_configured(channel_id: str, cfg: Any) -> bool:
    """Best-effort channel configuration check for UI display."""
    required: dict[str, list[str]] = {
        "telegram": ["token"],
        "whatsapp": ["bridge_token"],
        "discord": ["token"],
        "feishu": ["app_id", "app_secret"],
        "mochat": ["claw_token", "agent_user_id"],
        "dingtalk": ["client_id", "client_secret"],
        "email": [
            "imap_host",
            "imap_username",
            "imap_password",
            "smtp_host",
            "smtp_username",
            "smtp_password",
            "from_address",
        ],
        "slack": ["bot_token", "app_token"],
        "qq": ["app_id", "secret"],
        "matrix": ["access_token", "user_id"],
    }
    fields = required.get(channel_id, [])
    return all(bool(getattr(cfg, field, "")) for field in fields) if fields else False


def _channel_rows(config: Config, runtime_status: dict[str, Any]) -> list[dict[str, Any]]:
    """Return channel list with config + runtime status."""
    channels = [
        ("telegram", "Telegram"),
        ("discord", "Discord"),
        ("whatsapp", "WhatsApp"),
        ("feishu", "Feishu / Lark"),
        ("slack", "Slack"),
        ("dingtalk", "DingTalk"),
        ("qq", "QQ"),
        ("matrix", "Matrix"),
        ("email", "Email"),
        ("mochat", "Mochat"),
    ]

    rows: list[dict[str, Any]] = []
    for channel_id, label in channels:
        cfg = getattr(config.channels, channel_id)
        status = runtime_status.get(channel_id, {})
        rows.append(
            {
                "id": channel_id,
                "name": label,
                "enabled": bool(getattr(cfg, "enabled", False)),
                "configured": _channel_is_configured(channel_id, cfg),
                "running": bool(status.get("running", False)),
            }
        )
    return rows


class RuntimeEventBroker:
    """Fan-out broker for live runtime events."""

    def __init__(self, subscriber_queue_size: int = 500) -> None:
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._sequence = 0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        self._sequence += 1
        payload = {
            "id": self._sequence,
            "ts": int(time.time() * 1000),
            **event,
        }
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(dict(payload))
            except asyncio.QueueFull:
                continue


class CoreRuntime:
    """In-memory runtime used by the Studio API server."""

    def __init__(self, config: Config, host: str, port: int, with_channels: bool) -> None:
        self.config = config
        self.host = host
        self.port = port
        self.with_channels = with_channels
        self.started_at = 0.0

        sync_workspace_templates(config.workspace_path)
        self.bus = MessageBus()
        self.events = RuntimeEventBroker()
        self.provider = _make_provider(config)
        self.session_manager = SessionManager(config.workspace_path)
        self.cron = CronService(get_cron_dir() / "jobs.json")
        self.bus.add_inbound_listener(self._on_bus_inbound)
        self.bus.add_outbound_listener(self._on_bus_outbound)

        self.agent = AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            temperature=config.agents.defaults.temperature,
            max_tokens=config.agents.defaults.max_tokens,
            context_window_tokens=config.agents.defaults.context_window_tokens,
            max_iterations=config.agents.defaults.max_tool_iterations,
            memory_window=config.agents.defaults.memory_window,
            memory_config=config.agents.defaults.memory,
            compaction_config=config.agents.defaults.compaction,
            reasoning_effort=config.agents.defaults.reasoning_effort,
            brave_api_key=config.tools.web.search.api_key or None,
            web_search_provider=config.tools.web.search.provider,
            web_search_base_url=config.tools.web.search.base_url or None,
            web_search_max_results=config.tools.web.search.max_results,
            web_proxy=config.tools.web.proxy or None,
            exec_config=config.tools.exec,
            cron_service=self.cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=self.session_manager,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
        )

        self.cron.on_job = self._on_cron_job
        self.channels = ChannelManager(config, self.bus) if with_channels else None
        self._agent_task: asyncio.Task | None = None
        self._channels_task: asyncio.Task | None = None
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    def _on_bus_inbound(self, msg: Any) -> None:
        content = (msg.content or "").strip()
        preview = content[:280]
        event_type = "channel.message_received" if msg.channel not in {"studio", "cli", "system"} else "bus.inbound"
        self.events.publish(
            {
                "type": event_type,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "sender_id": msg.sender_id,
                "session_key": msg.session_key,
                "content": preview,
            }
        )

    def _on_bus_outbound(self, msg: Any) -> None:
        meta = msg.metadata or {}
        is_progress = bool(meta.get("_progress"))
        is_tool_hint = bool(meta.get("_tool_hint"))

        if is_progress:
            event_type = "agent.tool_hint" if is_tool_hint else "agent.progress"
        elif msg.channel not in {"studio", "cli", "system"}:
            event_type = "channel.reply_sent"
        else:
            event_type = "agent.reply_done"

        self.events.publish(
            {
                "type": event_type,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "session_key": f"{msg.channel}:{msg.chat_id}",
                "content": (msg.content or "")[:280],
                "progress": is_progress,
                "tool_hint": is_tool_hint,
            }
        )

    async def _on_cron_job(self, job: CronJob) -> str | None:
        """Execute a cron job with the same path used by direct chat."""
        return await self.agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )

    async def start(self) -> None:
        """Start runtime services."""
        async with self._lifecycle_lock:
            if self._started:
                return

            await self.cron.start()
            if self.with_channels:
                self._agent_task = asyncio.create_task(self.agent.run(), name="yuanclaw-agent-loop")
                self._channels_task = asyncio.create_task(
                    self.channels.start_all(), name="yuanclaw-channels"
                )
            self.started_at = time.time()
            self._started = True
            logger.info("Studio API runtime started on {}:{}", self.host, self.port)
            self.events.publish(
                {
                    "type": "core.started",
                    "host": self.host,
                    "port": self.port,
                    "pid": os.getpid(),
                    "with_channels": self.with_channels,
                }
            )

    async def stop(self) -> None:
        """Stop runtime services."""
        async with self._lifecycle_lock:
            if not self._started:
                return

            self.cron.stop()
            if self.channels is not None:
                await self.channels.stop_all()
            self.agent.stop()
            await self.agent.close_mcp()

            tasks = [t for t in (self._agent_task, self._channels_task) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            self._agent_task = None
            self._channels_task = None
            self._started = False
            self.events.publish({"type": "core.stopped"})
            logger.info("Studio API runtime stopped")

    @property
    def running(self) -> bool:
        return self._started

    def status_payload(self) -> dict[str, Any]:
        """Build dashboard status response."""
        channel_status = self.channels.get_status() if self.channels else {}
        channels = _channel_rows(self.config, channel_status)
        sessions = self.session_manager.list_sessions()
        skills = _list_skills()
        cron_jobs = self.cron.list_jobs(include_disabled=True)
        connected_channels = sum(1 for item in channels if item["running"])

        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
            "version": __version__,
            "model": self.config.agents.defaults.model,
            "workspace": str(self.config.workspace_path),
            "started_at": int(self.started_at),
            "uptime_sec": int(max(0, time.time() - self.started_at)) if self.started_at else 0,
            "sessions_count": len(sessions),
            "skills_count": len(skills),
            "channels": {
                "enabled": sum(1 for item in channels if item["enabled"]),
                "connected": connected_channels,
                "items": channels,
            },
            "cron": {
                "jobs": len(cron_jobs),
            },
        }


def create_app(runtime: CoreRuntime) -> FastAPI:
    """Create FastAPI application for local Studio runtime."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="YuanClaw Core API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5174",
            "http://127.0.0.1:5174",
            "tauri://localhost",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "running": runtime.running,
            "pid": os.getpid(),
            "version": __version__,
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return runtime.status_payload()

    @app.get("/api/skills")
    async def skills() -> dict[str, Any]:
        items = _list_skills()
        return {"items": items, "total": len(items)}

    @app.get("/api/channels")
    async def channels() -> dict[str, Any]:
        status_map = runtime.channels.get_status() if runtime.channels else {}
        items = _channel_rows(runtime.config, status_map)
        return {"items": items, "total": len(items)}

    @app.get("/api/cron/jobs")
    async def cron_jobs() -> dict[str, Any]:
        items = [asdict(job) for job in runtime.cron.list_jobs(include_disabled=True)]
        summary = {
            "total": len(items),
            "active": sum(1 for item in items if item["enabled"]),
            "paused": sum(1 for item in items if not item["enabled"]),
            "failed": sum(1 for item in items if item["state"]["last_status"] == "error"),
        }
        return {"items": items, "summary": summary}

    @app.get("/api/sessions")
    async def sessions() -> dict[str, Any]:
        items = runtime.session_manager.list_sessions()
        return {"items": items, "total": len(items)}

    @app.get("/api/sessions/{session_key:path}")
    async def session_detail(session_key: str) -> dict[str, Any]:
        key = unquote(session_key)
        if not key:
            raise HTTPException(status_code=400, detail="session key is required")

        session = runtime.session_manager.get_or_create(key)
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "last_consolidated": session.last_consolidated,
            "messages": session.messages,
        }

    @app.get("/api/config")
    async def read_config() -> dict[str, Any]:
        return {
            "model": runtime.config.agents.defaults.model,
            "workspace": str(runtime.config.workspace_path),
            "providers": _provider_rows(runtime.config),
            "raw": runtime.config.model_dump(by_alias=True),
        }

    @app.put("/api/config")
    async def write_config(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            next_config = Config.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid config payload: {exc}") from exc

        save_config(next_config)
        runtime.config = next_config
        return {
            "ok": True,
            "requires_restart": True,
            "saved_at": int(time.time()),
        }

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "ready"})

        while True:
            try:
                payload = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": f"invalid payload: {exc}"})
                continue

            event_type = str(payload.get("type") or "chat")
            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if event_type != "chat":
                await websocket.send_json({"type": "error", "message": f"unknown event type: {event_type}"})
                continue

            content = str(payload.get("content", "")).strip()
            if not content:
                await websocket.send_json({"type": "error", "message": "content is required"})
                continue

            session_key = str(payload.get("sessionKey") or "studio:default")
            if ":" in session_key:
                _, chat_id = session_key.split(":", 1)
            else:
                chat_id = session_key

            runtime.events.publish(
                {
                    "type": "studio.message_received",
                    "channel": "studio",
                    "chat_id": chat_id or "default",
                    "session_key": session_key,
                    "content": content[:280],
                }
            )

            progress_lines: list[str] = []
            started = time.time()

            async def on_progress(text: str, *, tool_hint: bool = False) -> None:
                progress_lines.append(text)
                runtime.events.publish(
                    {
                        "type": "agent.tool_hint" if tool_hint else "agent.progress",
                        "channel": "studio",
                        "chat_id": chat_id or "default",
                        "session_key": session_key,
                        "content": text[:280],
                        "progress": True,
                        "tool_hint": tool_hint,
                    }
                )
                await websocket.send_json(
                    {
                        "type": "progress",
                        "content": text,
                        "toolHint": tool_hint,
                        "ts": int(time.time()),
                    }
                )

            try:
                final = await runtime.agent.process_direct(
                    content=content,
                    session_key=session_key,
                    channel="studio",
                    chat_id=chat_id or "default",
                    on_progress=on_progress,
                )
                if not final:
                    final = "\n".join(progress_lines).strip()
                await websocket.send_json(
                    {
                        "type": "done",
                        "content": final,
                        "latencyMs": int((time.time() - started) * 1000),
                        "sessionKey": session_key,
                    }
                )
                runtime.events.publish(
                    {
                        "type": "agent.reply_done",
                        "channel": "studio",
                        "chat_id": chat_id or "default",
                        "session_key": session_key,
                        "content": final[:280],
                    }
                )
            except WebSocketDisconnect:
                break
            except Exception as exc:
                logger.exception("WS chat processing failed")
                await websocket.send_json({"type": "error", "message": str(exc)})

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        await websocket.accept()
        queue = runtime.events.subscribe()
        await websocket.send_json(
            {
                "type": "ready",
                "stream": "events",
                "ts": int(time.time() * 1000),
            }
        )
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            runtime.events.unsubscribe(queue)

    return app


def run_api_server(config: Config, host: str, port: int, with_channels: bool = False) -> None:
    """Run local Studio API server."""
    runtime = CoreRuntime(config=config, host=host, port=port, with_channels=with_channels)
    app = create_app(runtime)
    uvicorn.run(app, host=host, port=port, log_level="info")
