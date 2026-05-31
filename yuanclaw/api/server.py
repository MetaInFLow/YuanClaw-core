"""Local HTTP/WebSocket API used by YuanClaw Studio."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from loguru import logger

from yuanclaw import __version__
from yuanclaw.agent.loop import AgentLoop
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.manager import ChannelManager
from yuanclaw.config.loader import save_config
from yuanclaw.config.paths import get_cron_dir, get_media_dir
from yuanclaw.config.schema import Config
from yuanclaw.cron.service import CronService
from yuanclaw.cron.types import CronJob
from yuanclaw.providers.registry import PROVIDERS, find_by_name
from yuanclaw.session.goal_state import goal_state_ws_blob
from yuanclaw.session.manager import SessionManager
from yuanclaw.utils.helpers import sync_workspace_templates

CHANNEL_REQUIRED_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "telegram": (("token",),),
    "discord": (("token",),),
    "whatsapp": (("bridgeUrl",),),
    "feishu": (("appId",), ("appSecret",)),
    "slack": (("botToken",),),
    "dingtalk": (("clientId",), ("clientSecret",)),
    "qq": (("appId",), ("secret",)),
    "matrix": (("homeserver",), ("userId",), ("accessToken",)),
    "email": (
        ("fromAddress",),
        ("imapHost",),
        ("imapUsername",),
        ("imapPassword",),
        ("smtpHost",),
        ("smtpUsername",),
        ("smtpPassword",),
    ),
    "mochat": (("baseUrl",), ("clawToken",)),
}

_KNOWLEDGE_DISTILL_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "deliver_knowledge_distill",
            "description": "Return the structured run summary and durable insights for the Context Graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "headline": {
                        "type": "string",
                        "description": "Readable run headline shown directly on the summary node.",
                    },
                    "summaryPreview": {
                        "type": "string",
                        "description": "One-line preview shown before opening the modal.",
                    },
                    "summaryMarkdown": {
                        "type": "string",
                        "description": "Markdown body for the daily summary. Start with a heading and put key points before coverage/source sections.",
                    },
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        "project-context",
                                        "technical",
                                        "workflow",
                                        "rule",
                                        "preference",
                                        "other",
                                    ],
                                },
                                "summary": {"type": "string"},
                                "sourceSessionKeys": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "sourceExcerpt": {"type": "string"},
                            },
                            "required": ["title", "kind", "summary", "sourceSessionKeys"],
                        },
                    },
                },
                "required": ["headline", "summaryMarkdown", "insights"],
            },
        },
    }
]


def _value_at_path(source: Any, path: tuple[str, ...]) -> Any:
    cursor = source
    for segment in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(segment)
    return cursor


def _channel_field_has_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return any(_channel_field_has_value(item) for item in value)
    if isinstance(value, dict):
        return bool(value)
    return value is not None


def _channel_payload_config(payload: dict[str, Any], channel_id: str) -> dict[str, Any]:
    studio = payload.get("studio") if isinstance(payload.get("studio"), dict) else {}
    studio_channels = studio.get("channels") if isinstance(studio.get("channels"), dict) else {}
    if isinstance(studio_channels.get(channel_id), dict):
        return studio_channels[channel_id]

    runtime_channels = payload.get("channels") if isinstance(payload.get("channels"), dict) else {}
    if isinstance(runtime_channels.get(channel_id), dict):
        return runtime_channels[channel_id]

    return {}


def _channel_payload_requires_validation(
    payload: dict[str, Any], channel_id: str, channel_config: dict[str, Any]
) -> bool:
    studio = payload.get("studio") if isinstance(payload.get("studio"), dict) else {}
    channel_rows = studio.get("channelRows") if isinstance(studio.get("channelRows"), dict) else {}
    row_meta = channel_rows.get(channel_id) if isinstance(channel_rows.get(channel_id), dict) else {}
    if isinstance(row_meta.get("present"), bool):
        return bool(row_meta["present"])

    return bool(channel_config.get("enabled"))


def _channel_missing_required_fields(
    channel_id: str, channel_config: dict[str, Any]
) -> list[str]:
    missing: list[str] = []
    for path in CHANNEL_REQUIRED_FIELDS.get(channel_id, ()):
        if not _channel_field_has_value(_value_at_path(channel_config, path)):
            missing.append(".".join(path))
    return missing


def _validate_channel_payloads(payload: dict[str, Any]) -> None:
    for channel_id in CHANNEL_REQUIRED_FIELDS:
        channel_config = _channel_payload_config(payload, channel_id)
        if not _channel_payload_requires_validation(payload, channel_id, channel_config):
            continue

        missing = _channel_missing_required_fields(channel_id, channel_config)
        if missing:
            raise ValueError(
                f"channel `{channel_id}` is incomplete: missing required field(s): {', '.join(missing)}"
            )


_MEDIA_ALLOWED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
    "video/mp4",
    "video/webm",
    "video/quicktime",
}
_BYTE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


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
    from yuanclaw.providers.factory import make_provider

    return make_provider(config)


def _image_gen_provider_configs(config: Config):
    from yuanclaw.providers.image_generation import image_gen_provider_configs

    return image_gen_provider_configs(config)


def _mask_secret(value: str) -> str:
    """Mask secrets for UI display."""
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def _is_public_bind_host(host: str) -> bool:
    return host.strip() in {"0.0.0.0", "::"}


def _validate_public_bind_auth(host: str, gateway: Any) -> None:
    """Require auth material before binding the local API to all interfaces."""
    if not _is_public_bind_host(host):
        return
    if str(getattr(gateway, "token", "") or "").strip():
        return
    if str(getattr(gateway, "token_issue_secret", "") or "").strip():
        return
    raise RuntimeError(
        "host is 0.0.0.0 (all interfaces) but neither token nor "
        "token_issue_secret is set - set one to prevent unauthenticated access"
    )


def _gateway_auth_enabled(gateway: Any) -> bool:
    return bool(
        str(getattr(gateway, "token", "") or "").strip()
        or str(getattr(gateway, "token_issue_secret", "") or "").strip()
    )


def _request_token(connection: Request | WebSocket) -> str:
    auth = str(connection.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_token = str(connection.headers.get("x-yuanclaw-auth") or "").strip()
    if header_token:
        return header_token
    return str(connection.query_params.get("token") or "").strip()


def _issued_token_is_valid(
    token: str,
    issued_tokens: dict[str, float],
    *,
    now: float | None = None,
) -> bool:
    if not token:
        return False
    now = time.time() if now is None else now
    expires_at = issued_tokens.get(token)
    if expires_at is None:
        return False
    if expires_at <= now:
        issued_tokens.pop(token, None)
        return False
    return True


def _gateway_token_is_valid(
    token: str,
    gateway: Any,
    issued_tokens: dict[str, float],
) -> bool:
    static_token = str(getattr(gateway, "token", "") or "").strip()
    if static_token and hmac.compare_digest(token, static_token):
        return True
    return _issued_token_is_valid(token, issued_tokens)


def _token_issue_secret_is_valid(connection: Request, gateway: Any) -> bool:
    secret = str(getattr(gateway, "token_issue_secret", "") or "").strip()
    if not secret:
        return False
    provided = _request_token(connection)
    return bool(provided and hmac.compare_digest(provided, secret))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign_media_path(abs_path: Path, *, secret: bytes) -> str | None:
    try:
        media_root = get_media_dir(None).resolve()
        rel = abs_path.resolve().relative_to(media_root)
    except (OSError, ValueError):
        return None
    payload = _b64url_encode(rel.as_posix().encode("utf-8"))
    mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    return f"/api/media/{_b64url_encode(mac)}/{payload}"


def _augment_session_media_urls(payload: dict[str, Any], *, secret: bytes) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        media = message.get("media")
        if not isinstance(media, list) or not media:
            continue
        urls: list[dict[str, str]] = []
        for entry in media:
            if not isinstance(entry, str) or not entry:
                continue
            path = Path(entry)
            signed = _sign_media_path(path, secret=secret)
            if signed is not None:
                urls.append({"url": signed, "name": path.name})
        if urls:
            message["media_urls"] = urls
        message.pop("media", None)


def _parse_single_byte_range(range_header: str, size: int) -> tuple[int, int]:
    if size <= 0 or "," in range_header:
        raise ValueError("invalid byte range")
    match = _BYTE_RANGE_RE.fullmatch(range_header.strip())
    if match is None:
        raise ValueError("invalid byte range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("invalid byte range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid byte range")
        return max(size - suffix_length, 0), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or start > end:
        raise ValueError("invalid byte range")
    return start, min(end, size - 1)


def _serve_signed_media(
    sig: str,
    payload: str,
    *,
    secret: bytes,
    range_header: str | None = None,
) -> Response:
    try:
        provided_mac = _b64url_decode(sig)
    except (ValueError, binascii.Error):
        return Response("invalid signature", status_code=401)

    expected_mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected_mac, provided_mac):
        return Response("invalid signature", status_code=401)

    try:
        rel = _b64url_decode(payload).decode("utf-8")
        media_root = get_media_dir(None).resolve()
        candidate = (media_root / rel).resolve()
        candidate.relative_to(media_root)
    except (OSError, ValueError, binascii.Error, UnicodeDecodeError):
        return Response("not found", status_code=404)

    if not candidate.is_file():
        return Response("not found", status_code=404)

    mime, _ = mimetypes.guess_type(candidate.name)
    if mime not in _MEDIA_ALLOWED_MIMES:
        mime = "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if mime == "image/svg+xml":
        headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
        )

    try:
        size = candidate.stat().st_size
    except OSError:
        return Response("read error", status_code=500)

    if range_header:
        try:
            start, end = _parse_single_byte_range(range_header, size)
        except ValueError:
            return Response(
                "range not satisfiable",
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        try:
            with candidate.open("rb") as handle:
                handle.seek(start)
                body = handle.read(end - start + 1)
        except OSError:
            return Response("read error", status_code=500)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(body, status_code=206, media_type=mime, headers=headers)

    try:
        body = candidate.read_bytes()
    except OSError:
        return Response("read error", status_code=500)
    return Response(body, media_type=mime, headers=headers)


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


def _clean_single_line(value: Any, *, limit: int | None = None) -> str:
    cleaned = " ".join(str(value or "").split()).strip().strip("'\"`")
    if limit and len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if isinstance(arguments, list):
        if arguments and isinstance(arguments[0], dict):
            arguments = arguments[0]
        else:
            raise ValueError("knowledge distill tool returned invalid argument list")
    if not isinstance(arguments, dict):
        raise ValueError("knowledge distill tool returned non-object arguments")
    return arguments


def _classify_knowledge_insight_kind(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("偏好", "希望", "prefer", "preferred", "default")):
        return "preference"
    if any(token in lowered for token in ("必须", "需要", "should", "规则", "避免", "优先")):
        return "rule"
    if any(
        token in lowered
        for token in (
            "tauri",
            "reactflow",
            "sqlite",
            "schema",
            "prompt",
            "api",
            "markdown",
            "lark-cli",
            "feishu",
        )
    ):
        return "technical"
    if any(
        token in lowered
        for token in (
            "项目",
            "workspace",
            "obsidian",
            "context graph",
            "automation",
            "backend",
            "monorepo",
            "turborepo",
            "端口",
        )
    ):
        return "project-context"
    return "workflow"


def _normalize_knowledge_insight_kind(kind: Any, title: str, summary: str) -> str:
    normalized = _clean_single_line(kind)
    if normalized in {"project-context", "technical", "workflow", "rule", "preference", "other"}:
        return normalized
    return _classify_knowledge_insight_kind(f"{title} {summary}")


def _normalize_knowledge_source_keys(raw: Any, allowed_session_keys: set[str]) -> list[str]:
    if not isinstance(raw, list):
        return []
    items: list[str] = []
    for value in raw:
        key = _clean_single_line(value)
        if key and key in allowed_session_keys and key not in items:
            items.append(key)
    return items


def _first_markdown_signal(markdown: str) -> str:
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("> "):
            return line[2:].strip()
        if line.startswith("- "):
            return line[2:].strip()
        return line
    return ""


def _normalize_knowledge_distill_output(
    payload: dict[str, Any],
    allowed_session_keys: set[str],
) -> dict[str, Any]:
    headline = _clean_single_line(payload.get("headline"), limit=96)
    summary_preview = _clean_single_line(payload.get("summaryPreview"), limit=160) or None
    summary_markdown = str(payload.get("summaryMarkdown") or "").strip()
    if not summary_markdown:
        raise ValueError("knowledge distill output is missing summaryMarkdown")

    insights: list[dict[str, Any]] = []
    for raw in payload.get("insights") or []:
        if not isinstance(raw, dict):
            continue
        title = _clean_single_line(raw.get("title"), limit=64)
        summary = _clean_single_line(raw.get("summary"), limit=220)
        source_session_keys = _normalize_knowledge_source_keys(
            raw.get("sourceSessionKeys"), allowed_session_keys
        )
        if not title or not summary or not source_session_keys:
            continue
        source_excerpt = _clean_single_line(raw.get("sourceExcerpt"), limit=160) or None
        insights.append(
            {
                "title": title,
                "kind": _normalize_knowledge_insight_kind(raw.get("kind"), title, summary),
                "summary": summary,
                "sourceSessionKeys": source_session_keys,
                "sourceExcerpt": source_excerpt,
            }
        )

    if not headline:
        headline = summary_preview or (insights[0]["title"] if insights else "") or _first_markdown_signal(summary_markdown)
    if not headline:
        raise ValueError("knowledge distill output is missing headline")
    if not summary_preview:
        summary_preview = (insights[0]["summary"] if insights else "") or headline
    if not summary_markdown.startswith("#"):
        summary_markdown = f"# {headline}\n\n{summary_markdown}"

    return {
        "headline": headline,
        "summaryPreview": summary_preview,
        "summaryMarkdown": summary_markdown,
        "insights": insights,
    }


def _render_message_content(content: Any) -> str:
    if isinstance(content, str):
        return _clean_single_line(content, limit=280)
    if isinstance(content, list):
        rendered = " ".join(_render_message_content(item) for item in content)
        return _clean_single_line(rendered, limit=280)
    if isinstance(content, dict):
        return _clean_single_line(json.dumps(content, ensure_ascii=False), limit=280)
    return _clean_single_line(content, limit=280)


def _render_session_messages(messages: list[dict[str, Any]], limit: int = 10) -> str:
    rendered: list[str] = []
    for message in messages[-limit:]:
        role = _clean_single_line(message.get("role")).upper() or "UNKNOWN"
        content = _render_message_content(message.get("content"))
        if not content:
            continue
        rendered.append(f"- {role}: {content}")
    return "\n".join(rendered) if rendered else "- (no persisted messages)"


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
        _validate_public_bind_auth(host, config.gateway)
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
            image_generation_config=config.tools.image_generation,
            image_generation_provider_configs=_image_gen_provider_configs(config),
            cli_apps_config=config.tools.cli_apps,
            max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
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
                image_generation_config=config.tools.image_generation,
                image_generation_provider_configs=_image_gen_provider_configs(config),
                cli_apps_config=config.tools.cli_apps,
                max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
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
        _validate_public_bind_auth(self.host, config.gateway)
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

    async def distill_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return structured daily knowledge distill output for Studio."""
        if not _has_summary_model_access(self.config):
            raise RuntimeError("summary model is unavailable")

        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        if not sessions:
            raise ValueError("sessions are required")

        existing_insights = (
            payload.get("existingInsights")
            if isinstance(payload.get("existingInsights"), list)
            else []
        )
        allowed_session_keys: set[str] = set()
        session_sections: list[str] = []

        for raw in sessions[:40]:
            if not isinstance(raw, dict):
                continue
            session_key = _clean_single_line(raw.get("sessionKey"))
            if not session_key:
                continue
            allowed_session_keys.add(session_key)
            thread_summary = _clean_single_line(raw.get("threadSummary"), limit=180) or "(none)"
            last_preview = _clean_single_line(raw.get("lastMessagePreview"), limit=220) or "(none)"
            updated_at = _clean_single_line(raw.get("updatedAt")) or "(unknown)"
            live_session = self.session_manager.get_or_create(session_key)
            recent_turns = _render_session_messages(live_session.messages)
            session_sections.append(
                "\n".join(
                    [
                        f"## Session {session_key}",
                        f"- Updated at: {updated_at}",
                        f"- Thread summary: {thread_summary}",
                        f"- Last preview: {last_preview}",
                        "- Recent turns:",
                        recent_turns,
                    ]
                )
            )

        if not allowed_session_keys:
            raise ValueError("no valid session keys were supplied")

        existing_lines: list[str] = []
        for raw in existing_insights[:80]:
            if not isinstance(raw, dict):
                continue
            title = _clean_single_line(raw.get("title"), limit=80)
            if not title:
                continue
            kind = _clean_single_line(raw.get("kind"), limit=32) or "workflow"
            summary = _clean_single_line(raw.get("summary"), limit=200)
            merge_key = _clean_single_line(raw.get("mergeKey"), limit=120)
            existing_lines.append(
                f"- [{kind}] {title} :: {summary or '(no summary)'} :: mergeKey={merge_key or '(none)'}"
            )

        response = await self.provider.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你在为 Studio Context Graph 提炼可复用知识。"
                        "只调用 deliver_knowledge_distill 工具，不要输出自由文本。"
                        "目标：产出一个可直接显示在图上的 run 级 headline/summary 和 durable insights。"
                        "禁止输出这些低价值内容：确认词、编号残片、shell/file listing、权限串、路径或 ID dump、timeout/status-only 文本。"
                        "只保留可跨会话复用的事实、约束、工作流、偏好、项目背景。"
                        "summaryMarkdown 必须先给 headline/关键点，再放 coverage/source sessions。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Run date: {_clean_single_line(payload.get('runDate')) or '(unknown)'}\n\n"
                        "Collection prompt:\n"
                        f"{str(payload.get('collectionPrompt') or '').strip() or '(empty)'}\n\n"
                        "System prompt:\n"
                        f"{str(payload.get('systemPrompt') or '').strip() or '(empty)'}\n\n"
                        "Archive prompt:\n"
                        f"{str(payload.get('archivePrompt') or '').strip() or '(empty)'}\n\n"
                        "Existing durable insights:\n"
                        f"{chr(10).join(existing_lines) if existing_lines else '- (none)'}\n\n"
                        "Eligible sessions:\n"
                        f"{chr(10).join(session_sections)}"
                    ),
                },
            ],
            tools=_KNOWLEDGE_DISTILL_TOOL,
            model=self.config.agents.defaults.model,
            max_tokens=self.config.agents.defaults.max_tokens,
            temperature=0.2,
            reasoning_effort=self.config.agents.defaults.reasoning_effort,
        )

        if not response.has_tool_calls:
            raise RuntimeError("model did not return structured knowledge distill output")

        args = _normalize_tool_arguments(response.tool_calls[0].arguments)
        return _normalize_knowledge_distill_output(args, allowed_session_keys)


def create_app(runtime: CoreRuntime) -> FastAPI:
    """Create FastAPI application for local Studio runtime."""
    oauth_manager = OAuthLoginManager()
    issued_tokens: dict[str, float] = {}
    media_secret = token_urlsafe(32).encode("utf-8")

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

    @app.middleware("http")
    async def gateway_token_middleware(request: Request, call_next):
        gateway = runtime.config.gateway
        token_issue_path = str(gateway.token_issue_path or "").rstrip("/") or ""
        request_path = request.url.path.rstrip("/") or "/"
        if request.method == "OPTIONS" or request_path == "/health":
            return await call_next(request)
        if token_issue_path and request_path == token_issue_path:
            return await call_next(request)
        if not _gateway_auth_enabled(gateway):
            return await call_next(request)
        token = _request_token(request)
        if not _gateway_token_is_valid(token, gateway, issued_tokens):
            return JSONResponse({"detail": "gateway token is required"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "running": runtime.running,
            "pid": os.getpid(),
            "version": __version__,
        }

    @app.get(runtime.config.gateway.token_issue_path or "/api/auth/token")
    async def issue_gateway_token(request: Request) -> dict[str, Any]:
        gateway = runtime.config.gateway
        if not _token_issue_secret_is_valid(request, gateway):
            raise HTTPException(status_code=401, detail="token issue secret is required")

        now = time.time()
        for token, expires_at in list(issued_tokens.items()):
            if expires_at <= now:
                issued_tokens.pop(token, None)

        token = token_urlsafe(32)
        ttl = int(gateway.token_ttl_s)
        issued_tokens[token] = now + ttl
        return {"token": token, "expires_in": ttl}

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

    @app.post("/api/internal/knowledge/distill")
    async def knowledge_distill(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await runtime.distill_knowledge(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_key:path}/messages")
    async def session_messages(session_key: str) -> dict[str, Any]:
        key = unquote(session_key)
        if not key:
            raise HTTPException(status_code=400, detail="session key is required")

        data = runtime.session_manager.read_session_file(key)
        if data is None:
            raise HTTPException(status_code=404, detail="session not found")
        _augment_session_media_urls(data, secret=media_secret)
        return data

    @app.get("/api/sessions/{session_key:path}")
    async def session_detail(session_key: str) -> dict[str, Any]:
        key = unquote(session_key)
        if not key:
            raise HTTPException(status_code=400, detail="session key is required")

        data = runtime.session_manager.read_session_file(key)
        if data is None:
            raise HTTPException(status_code=404, detail="session not found")
        _augment_session_media_urls(data, secret=media_secret)
        return data

    @app.get("/api/media/{sig}/{payload}")
    async def signed_media(sig: str, payload: str, request: Request) -> Response:
        return _serve_signed_media(
            sig,
            payload,
            secret=media_secret,
            range_header=request.headers.get("range"),
        )

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
            _validate_channel_payloads(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid config payload: {exc}") from exc

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
        if _gateway_auth_enabled(runtime.config.gateway):
            token = _request_token(websocket)
            if not _gateway_token_is_valid(token, runtime.config.gateway, issued_tokens):
                await websocket.close(code=1008)
                return
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
            runtime_metadata: dict[str, Any] = {}
            if payload.get("cliApps") is not None:
                runtime_metadata["cliApps"] = payload.get("cliApps")
            if payload.get("mcpPresets") is not None:
                runtime_metadata["mcpPresets"] = payload.get("mcpPresets")
            workspace_scope = payload.get("workspaceScope")
            if workspace_scope is None:
                workspace_scope = payload.get("workspace_scope")
            if isinstance(workspace_scope, dict):
                runtime_metadata["workspace_scope"] = {
                    "project_path": workspace_scope.get("project_path")
                    or workspace_scope.get("projectPath")
                    or workspace_scope.get("path"),
                    "access_mode": workspace_scope.get("access_mode")
                    or workspace_scope.get("accessMode"),
                }
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

            async def on_progress(
                text: str,
                *,
                tool_hint: bool = False,
                tool_events: list[dict[str, Any]] | None = None,
                file_edit_events: list[dict[str, Any]] | None = None,
                **_kwargs: Any,
            ) -> None:
                progress_lines.append(text)
                event = {
                    "type": "agent.tool_hint" if tool_hint else "agent.progress",
                    "channel": "studio",
                    "chat_id": chat_id or "default",
                    "session_key": session_key,
                    "content": text[:280],
                    "progress": True,
                    "tool_hint": tool_hint,
                }
                frame = {
                    "type": "progress",
                    "content": text,
                    "toolHint": tool_hint,
                    "ts": int(time.time()),
                }
                if tool_events:
                    event["tool_events"] = tool_events
                    frame["toolEvents"] = tool_events
                if file_edit_events:
                    event["file_edit_events"] = file_edit_events
                    frame["fileEditEvents"] = file_edit_events
                runtime.events.publish(event)
                await websocket.send_json(frame)

            try:
                await websocket.send_json(
                    {
                        "type": "goal_status",
                        "status": "running",
                        "startedAt": started,
                        "sessionKey": session_key,
                    }
                )
                runtime.events.publish(
                    {
                        "type": "agent.goal_status",
                        "status": "running",
                        "started_at": started,
                        "channel": "studio",
                        "chat_id": chat_id or "default",
                        "session_key": session_key,
                    }
                )
                final = await runtime.agent.process_direct(
                    content=content,
                    session_key=session_key,
                    channel="studio",
                    chat_id=chat_id or "default",
                    skill_names=skill_names,
                    on_progress=on_progress,
                    metadata=runtime_metadata,
                )
                if not final:
                    final = "\n".join(progress_lines).strip()
                session_data = runtime.session_manager.read_session_file(session_key) or {}
                goal_state = goal_state_ws_blob(session_data.get("metadata"))
                await websocket.send_json(
                    {
                        "type": "done",
                        "content": final,
                        "latencyMs": int((time.time() - started) * 1000),
                        "sessionKey": session_key,
                        "goalState": goal_state,
                    }
                )
                turn_end = {
                    "type": "turn_end",
                    "status": "idle",
                    "latencyMs": int((time.time() - started) * 1000),
                    "sessionKey": session_key,
                    "goalState": goal_state,
                }
                await websocket.send_json(turn_end)
                runtime.events.publish(
                    {
                        "type": "agent.reply_done",
                        "channel": "studio",
                        "chat_id": chat_id or "default",
                        "session_key": session_key,
                        "content": final[:280],
                        "goal_state": goal_state,
                    }
                )
                runtime.events.publish(
                    {
                        "type": "agent.turn_end",
                        "status": "idle",
                        "channel": "studio",
                        "chat_id": chat_id or "default",
                        "session_key": session_key,
                        "latency_ms": turn_end["latencyMs"],
                        "goal_state": goal_state,
                    }
                )
            except WebSocketDisconnect:
                break
            except Exception as exc:
                logger.exception("WS chat processing failed")
                await websocket.send_json({"type": "error", "message": str(exc)})

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        if _gateway_auth_enabled(runtime.config.gateway):
            token = _request_token(websocket)
            if not _gateway_token_is_valid(token, runtime.config.gateway, issued_tokens):
                await websocket.close(code=1008)
                return
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
