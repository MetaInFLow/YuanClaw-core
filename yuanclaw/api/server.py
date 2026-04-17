"""Local HTTP/WebSocket API used by YuanClaw Studio."""

from __future__ import annotations

import asyncio
import os
import urllib.parse
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from secrets import token_urlsafe
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


@dataclass
class OAuthLoginSession:
    """Transient OAuth browser-login state for desktop settings."""

    provider_id: str
    session_id: str
    authorize_url: str
    state: str
    verifier: str
    started_at_ms: int
    updated_at_ms: int
    status: str = "waiting_browser"
    message: str = "Waiting for browser authorization."
    account_id: str | None = None
    expires_at_ms: int | None = None
    server: Any = field(default=None, repr=False)
    code_future: asyncio.Future[str] | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "session_id": self.session_id,
            "authorize_url": self.authorize_url,
            "started_at_ms": self.started_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "status": self.status,
            "message": self.message,
            "account_id": self.account_id,
            "expires_at_ms": self.expires_at_ms,
        }


class OAuthLoginManager:
    """Manage desktop OAuth state without requiring a terminal login."""

    def __init__(self) -> None:
        self._sessions: dict[str, OAuthLoginSession] = {}

    @staticmethod
    def _provider_definition(provider_id: str):
        if provider_id != "openai_codex":
            return None

        from oauth_cli_kit.constants import OPENAI_CODEX_PROVIDER

        return OPENAI_CODEX_PROVIDER

    def _storage(self, provider_id: str):
        from oauth_cli_kit.storage import FileTokenStorage

        provider = self._provider_definition(provider_id)
        if provider is None:
            raise RuntimeError(f"Unsupported OAuth provider: {provider_id}")
        return FileTokenStorage(token_filename=provider.token_filename)

    def _token_status(self, provider_id: str, *, refresh: bool) -> dict[str, Any]:
        provider = self._provider_definition(provider_id)
        if provider is None:
            return {
                "provider_id": provider_id,
                "supported": False,
                "configured": False,
                "state": "unsupported",
                "message": "Desktop OAuth flow is not implemented for this provider yet.",
                "account_id": None,
                "expires_at_ms": None,
                "active_session": None,
            }

        from oauth_cli_kit import get_token

        storage = self._storage(provider_id)
        token = storage.load()
        if not token:
            return {
                "provider_id": provider_id,
                "supported": True,
                "configured": False,
                "state": "missing",
                "message": "Not signed in.",
                "account_id": None,
                "expires_at_ms": None,
                "active_session": None,
            }

        now_ms = int(time.time() * 1000)
        if token.expires - now_ms > 60 * 1000:
            return {
                "provider_id": provider_id,
                "supported": True,
                "configured": True,
                "state": "connected",
                "message": "OAuth session is ready.",
                "account_id": token.account_id,
                "expires_at_ms": token.expires,
                "active_session": None,
            }

        if not refresh:
            return {
                "provider_id": provider_id,
                "supported": True,
                "configured": False,
                "state": "expiring",
                "message": "Stored token is expiring and needs refresh.",
                "account_id": token.account_id,
                "expires_at_ms": token.expires,
                "active_session": None,
            }

        try:
            refreshed = get_token(provider=provider, storage=storage)
            return {
                "provider_id": provider_id,
                "supported": True,
                "configured": True,
                "state": "connected",
                "message": "OAuth session is ready.",
                "account_id": refreshed.account_id,
                "expires_at_ms": refreshed.expires,
                "active_session": None,
            }
        except Exception as exc:
            return {
                "provider_id": provider_id,
                "supported": True,
                "configured": False,
                "state": "reauth_required",
                "message": str(exc),
                "account_id": token.account_id,
                "expires_at_ms": token.expires,
                "active_session": None,
            }

    async def start_login(self, provider_id: str) -> dict[str, Any]:
        provider = self._provider_definition(provider_id)
        if provider is None:
            raise RuntimeError(f"Unsupported OAuth provider: {provider_id}")

        current = self._sessions.get(provider_id)
        if current and current.status in {"waiting_browser", "exchanging"}:
            return self.status(provider_id)

        from oauth_cli_kit.flow import _create_state, _exchange_code_for_token_async, _generate_pkce
        from oauth_cli_kit.server import _start_local_server

        verifier, challenge = _generate_pkce()
        state = _create_state()
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "scope": provider.scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": provider.default_originator,
        }
        authorize_url = f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"

        loop = asyncio.get_running_loop()
        code_future: asyncio.Future[str] = loop.create_future()

        def _notify(code_value: str) -> None:
            if code_future.done():
                return
            loop.call_soon_threadsafe(code_future.set_result, code_value)

        server, server_error = _start_local_server(state, on_code=_notify)
        if not server:
            raise RuntimeError(server_error or "Failed to start local OAuth callback server.")

        session = OAuthLoginSession(
            provider_id=provider_id,
            session_id=token_urlsafe(12),
            authorize_url=authorize_url,
            state=state,
            verifier=verifier,
            started_at_ms=int(time.time() * 1000),
            updated_at_ms=int(time.time() * 1000),
            server=server,
            code_future=code_future,
        )
        self._sessions[provider_id] = session

        async def _complete_login() -> None:
            try:
                code = await asyncio.wait_for(code_future, timeout=300)
                session.status = "exchanging"
                session.updated_at_ms = int(time.time() * 1000)
                session.message = "Browser callback received. Exchanging tokens..."
                token = await _exchange_code_for_token_async(code, verifier, provider)()
                self._storage(provider_id).save(token)
                session.status = "success"
                session.updated_at_ms = int(time.time() * 1000)
                session.message = "OAuth login completed."
                session.account_id = token.account_id
                session.expires_at_ms = token.expires
            except asyncio.TimeoutError:
                session.status = "error"
                session.updated_at_ms = int(time.time() * 1000)
                session.message = "Timed out waiting for the browser callback. Please try again."
            except Exception as exc:
                session.status = "error"
                session.updated_at_ms = int(time.time() * 1000)
                session.message = str(exc)
            finally:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:
                    pass

        session.task = asyncio.create_task(_complete_login(), name=f"oauth-login-{provider_id}")
        return self.status(provider_id)

    def status(self, provider_id: str) -> dict[str, Any]:
        status = self._token_status(provider_id, refresh=True)
        session = self._sessions.get(provider_id)
        if session is None:
            return status

        status["active_session"] = session.payload()
        if session.status in {"waiting_browser", "exchanging"}:
            status["configured"] = False
            status["state"] = session.status
            status["message"] = session.message
        elif session.status == "error":
            status["configured"] = False
            status["state"] = "reauth_required"
            status["message"] = session.message
        elif session.status == "success":
            status["configured"] = True
            status["state"] = "connected"
            status["message"] = session.message
            status["account_id"] = session.account_id
            status["expires_at_ms"] = session.expires_at_ms

        return status

    def logout(self, provider_id: str) -> dict[str, Any]:
        provider = self._provider_definition(provider_id)
        if provider is None:
            raise RuntimeError(f"Unsupported OAuth provider: {provider_id}")

        session = self._sessions.get(provider_id)
        if session is not None:
            if session.task and not session.task.done():
                session.task.cancel()
            try:
                if session.server:
                    session.server.shutdown()
                    session.server.server_close()
            except Exception:
                pass
            self._sessions.pop(provider_id, None)

        token_path = self._storage(provider_id).get_token_path()
        try:
            if token_path.exists():
                token_path.unlink()
        except Exception as exc:
            raise RuntimeError(f"Failed to remove OAuth token: {exc}") from exc

        return self.status(provider_id)


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
            extra_headers=p.extra_headers if p else None,
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

    if provider_name == "ovms":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v3",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )

    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
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


def _provider_rows(
    config: Config,
    oauth_statuses: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Summarize providers for Settings page."""
    selected = config.get_provider_name(config.agents.defaults.model)
    rows: list[dict[str, Any]] = []

    for spec in PROVIDERS:
        provider = getattr(config.providers, spec.name, None)
        if provider is None:
            continue

        oauth_status = (oauth_statuses or {}).get(spec.name, {})
        configured = (
            bool(oauth_status.get("configured")) if oauth_status.get("supported") else True
        ) if spec.is_oauth else bool(provider.api_key or provider.api_base)
        rows.append(
            {
                "id": spec.name,
                "name": spec.label,
                "configured": configured,
                "is_default": spec.name == selected,
                "api_key_masked": _mask_secret(provider.api_key or ""),
                "api_base": provider.api_base or "",
                "oauth_supported": bool(oauth_status.get("supported")) if spec.is_oauth else False,
                "oauth_state": oauth_status.get("state") if spec.is_oauth else None,
                "oauth_message": oauth_status.get("message") if spec.is_oauth else None,
                "oauth_account_id": oauth_status.get("account_id") if spec.is_oauth else None,
                "oauth_expires_at_ms": oauth_status.get("expires_at_ms") if spec.is_oauth else None,
            }
        )

    return rows


def _has_summary_model_access(config: Config) -> bool:
    """Return whether the active model is likely callable for summary generation."""
    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    provider = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None

    if model.startswith("bedrock/"):
        return True
    if spec and spec.is_oauth:
        return True
    if provider_name == "custom":
        return bool((provider and provider.api_key) or config.get_api_base(model))
    if provider_name == "azure_openai":
        return bool(provider and provider.api_key and provider.api_base)
    return bool(provider and provider.api_key)


def _compact_thread_summary(text: str, limit: int = 24) -> str:
    """Return a compact single-line thread summary."""
    normalized = " ".join(str(text or "").split()).strip().strip("'\"`")
    if not normalized:
        return "新对话"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _normalize_thread_summary(raw: str | None, fallback: str) -> str:
    """Normalize model output for thread list display."""
    cleaned = " ".join(str(raw or "").split()).strip().strip("'\"`")
    if not cleaned:
        return fallback
    return _compact_thread_summary(cleaned)


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
        self.host = host
        self.port = port
        self.with_channels = with_channels
        self.started_at = 0.0
        self.events = RuntimeEventBroker()
        self.config = config
        self.bus = MessageBus()
        self.provider = _make_provider(config)
        self.session_manager = SessionManager(config.workspace_path)
        self.cron = CronService(get_cron_dir() / "jobs.json")
        self.agent = AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            provider_name=config.get_provider_name(config.agents.defaults.model),
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
        self.channels = None
        self._agent_task: asyncio.Task | None = None
        self._channels_task: asyncio.Task | None = None
        self._started = False
        self._lifecycle_lock = asyncio.Lock()
        self._install_components(config, self._create_components(config))

    def _create_components(self, config: Config) -> dict[str, Any]:
        """Build runtime components for a config without mutating the active runtime."""
        try:
            sync_workspace_templates(config.workspace_path)

            bus = MessageBus()
            bus.add_inbound_listener(self._on_bus_inbound)
            bus.add_outbound_listener(self._on_bus_outbound)

            provider = _make_provider(config)
            session_manager = SessionManager(config.workspace_path)
            cron = CronService(get_cron_dir() / "jobs.json")
            agent = AgentLoop(
                bus=bus,
                provider=provider,
                workspace=config.workspace_path,
                model=config.agents.defaults.model,
                provider_name=config.get_provider_name(config.agents.defaults.model),
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
                cron_service=cron,
                restrict_to_workspace=config.tools.restrict_to_workspace,
                session_manager=session_manager,
                mcp_servers=config.tools.mcp_servers,
                channels_config=config.channels,
            )
            cron.on_job = self._on_cron_job
            channels = ChannelManager(config, bus) if self.with_channels else None
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise RuntimeError(f"Failed to prepare runtime components: {exc}") from exc

        return {
            "bus": bus,
            "provider": provider,
            "session_manager": session_manager,
            "cron": cron,
            "agent": agent,
            "channels": channels,
        }

    def _install_components(self, config: Config, components: dict[str, Any]) -> None:
        """Swap in a fully prepared runtime component set."""
        self.config = config
        self.bus = components["bus"]
        self.provider = components["provider"]
        self.session_manager = components["session_manager"]
        self.cron = components["cron"]
        self.agent = components["agent"]
        self.channels = components["channels"]

    def prepare_config(self, config: Config) -> dict[str, Any]:
        """Validate whether a config can be applied to the running Core."""
        return self._create_components(config)

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

    async def _start_locked(self) -> None:
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

    async def start(self) -> None:
        """Start runtime services."""
        async with self._lifecycle_lock:
            await self._start_locked()

    async def _stop_locked(self) -> None:
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

    async def stop(self) -> None:
        """Stop runtime services."""
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def apply_config(
        self,
        config: Config,
        prepared_components: dict[str, Any] | None = None,
    ) -> None:
        """Hot-apply config so provider/model/channel changes take effect immediately."""
        components = prepared_components or self._create_components(config)

        async with self._lifecycle_lock:
            was_running = self._started
            if was_running:
                await self._stop_locked()

            self._install_components(config, components)

            if was_running:
                await self._start_locked()

        self.events.publish(
            {
                "type": "core.reconfigured",
                "model": config.agents.defaults.model,
                "workspace": str(config.workspace_path),
            }
        )
        logger.info(
            "Studio API runtime reconfigured: model={}, workspace={}",
            config.agents.defaults.model,
            config.workspace_path,
        )

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

    async def generate_thread_summary(
        self,
        session_key: str,
        content: str,
        cowboy_name: str | None = None,
    ) -> dict[str, str]:
        """Generate and persist a thread summary for Studio UI."""
        fallback = _compact_thread_summary(content)

        if not _has_summary_model_access(self.config):
            self.session_manager.set_thread_summary(session_key, fallback)
            return {"summary": fallback, "mode": "input"}

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你在为桌面应用生成线程列表标题。"
                            "请根据用户开启任务时的首条输入，为当前角色写一句简短概述。"
                            "要求：只输出标题文本；中文优先；不要换行、不要引号、不要序号、不要解释；"
                            "长度控制在 8 到 18 个中文字符或 24 个字符以内。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"角色：{cowboy_name or 'AI 助手'}\n"
                            f"用户输入：{content}\n"
                            "输出："
                        ),
                    },
                ],
                model=self.config.agents.defaults.model,
                max_tokens=64,
                temperature=0.2,
                reasoning_effort=self.config.agents.defaults.reasoning_effort,
            )
            summary = _normalize_thread_summary(response.content, fallback)
            self.session_manager.set_thread_summary(session_key, summary)
            return {"summary": summary, "mode": "llm"}
        except Exception as exc:
            logger.warning("Thread summary generation failed for {}: {}", session_key, exc)
            self.session_manager.set_thread_summary(session_key, fallback)
            return {"summary": fallback, "mode": "input"}


def create_app(runtime: CoreRuntime) -> FastAPI:
    """Create FastAPI application for local Studio runtime."""
    oauth_manager = OAuthLoginManager()

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

    @app.post("/api/sessions/{session_key:path}/summary")
    async def session_summary(session_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = unquote(session_key)
        if not key:
            raise HTTPException(status_code=400, detail="session key is required")

        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")

        cowboy_name = str(payload.get("cowboyName") or "").strip() or None
        return await runtime.generate_thread_summary(
            session_key=key,
            content=content,
            cowboy_name=cowboy_name,
        )

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
        oauth_statuses = {
            spec.name: oauth_manager.status(spec.name)
            for spec in PROVIDERS
            if spec.is_oauth
        }
        return {
            "model": runtime.config.agents.defaults.model,
            "workspace": str(runtime.config.workspace_path),
            "providers": _provider_rows(runtime.config, oauth_statuses=oauth_statuses),
            "raw": runtime.config.model_dump(by_alias=True),
        }

    @app.get("/api/usage")
    async def usage() -> dict[str, Any]:
        return runtime.session_manager.summarize_usage()

    @app.get("/api/providers/oauth/{provider_id}")
    async def oauth_provider_status(provider_id: str) -> dict[str, Any]:
        try:
            return oauth_manager.status(provider_id.replace("-", "_"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/providers/oauth/{provider_id}/start")
    async def oauth_provider_start(provider_id: str) -> dict[str, Any]:
        try:
            return await oauth_manager.start_login(provider_id.replace("-", "_"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/providers/oauth/{provider_id}/logout")
    async def oauth_provider_logout(provider_id: str) -> dict[str, Any]:
        try:
            return oauth_manager.logout(provider_id.replace("-", "_"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/config")
    async def write_config(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            next_config = Config.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid config payload: {exc}") from exc

        try:
            prepared_components = runtime.prepare_config(next_config)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"config is not applicable: {exc}") from exc

        try:
            save_config(next_config)
            await runtime.apply_config(next_config, prepared_components=prepared_components)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to apply config: {exc}") from exc

        return {
            "ok": True,
            "requires_restart": False,
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
            skill_names = [
                str(name).strip()
                for name in (payload.get("skillNames") or [])
                if str(name).strip()
            ]
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
                    skill_names=skill_names,
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
