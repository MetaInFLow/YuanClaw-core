"""CLI Apps helpers shared by channels and agent runtime context."""

from __future__ import annotations

import re
from typing import Any, Mapping

_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def _safe_name(value: Any) -> str:
    raw = str(value or "").strip()
    if "/" in raw or "\\" in raw or ".." in raw:
        return ""
    return _SAFE_NAME_RE.sub("-", raw.lower()).strip("-")


def normalize_cli_app_mentions(raw: Any) -> list[dict[str, str]]:
    """Normalize structured CLI app attachments from API/WebSocket payloads."""
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(item.get("name"))
        if not name or name in seen:
            continue
        entry_point = str(item.get("entry_point") or item.get("entryPoint") or "").strip()
        display_name = str(item.get("display_name") or item.get("displayName") or "").strip()
        payload = {"name": name}
        if display_name:
            payload["display_name"] = display_name
        if entry_point:
            payload["entry_point"] = entry_point
        normalized.append(payload)
        seen.add(name)
    return normalized


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    cli_apps = metadata.get("cli_apps") if isinstance(metadata, Mapping) else None
    return {"cli_apps": cli_apps} if isinstance(cli_apps, list) and cli_apps else {}


def cli_app_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    structured = metadata.get("cli_apps") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(item.get("name"))
        if not name:
            continue
        entry_point = str(item.get("entry_point") or "unknown").strip() or "unknown"
        lines.append(
            "CLI App Attachment: "
            f"@{name} (installed; tool=run_cli_app; entry_point={entry_point}; "
            f"skill=skills/cli-app-{name}/SKILL.md). "
            "Read the skill when useful, then run this app with `run_cli_app`; "
            "do not bypass it with shell."
        )
    return lines
