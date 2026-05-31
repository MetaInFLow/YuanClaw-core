"""Small JSON pairing store for DM sender approval."""

from __future__ import annotations

import json
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any

from yuanclaw.config.paths import get_data_dir

_LOCK = threading.Lock()
_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 8


def _store_path() -> Path:
    return get_data_dir() / "pairing.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"approved": {}, "pending": {}}
    approved = data.setdefault("approved", {})
    for channel, users in list(approved.items()):
        approved[channel] = set(users or [])
    data.setdefault("pending", {})
    return data


def _save(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "approved": {
            channel: sorted(users)
            for channel, users in data.get("approved", {}).items()
        },
        "pending": dict(data.get("pending", {})),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _gc_pending(data: dict[str, Any]) -> None:
    now = time.time()
    pending = data.get("pending", {})
    for code, info in list(pending.items()):
        if float(info.get("expires_at", 0) or 0) <= now:
            pending.pop(code, None)


def generate_code(channel: str, sender_id: str, ttl: int = 600) -> str:
    with _LOCK:
        data = _load()
        _gc_pending(data)
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        code = f"{raw[:4]}-{raw[4:]}"
        data.setdefault("pending", {})[code] = {
            "channel": channel,
            "sender_id": sender_id,
            "created_at": time.time(),
            "expires_at": time.time() + ttl,
        }
        _save(data)
        return code


def approve_code(code: str) -> tuple[str, str] | None:
    with _LOCK:
        data = _load()
        _gc_pending(data)
        info = data.get("pending", {}).pop(code, None)
        if not info:
            _save(data)
            return None
        channel = str(info["channel"])
        sender_id = str(info["sender_id"])
        data.setdefault("approved", {}).setdefault(channel, set()).add(sender_id)
        _save(data)
        return channel, sender_id


def deny_code(code: str) -> bool:
    with _LOCK:
        data = _load()
        existed = code in data.get("pending", {})
        data.get("pending", {}).pop(code, None)
        _save(data)
        return existed


def is_approved(channel: str, sender_id: str) -> bool:
    with _LOCK:
        data = _load()
        return str(sender_id) in data.get("approved", {}).get(channel, set())


def list_pending() -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
        _gc_pending(data)
        _save(data)
        return [{"code": code, **info} for code, info in data.get("pending", {}).items()]


def revoke(channel: str, sender_id: str) -> bool:
    with _LOCK:
        data = _load()
        users = data.get("approved", {}).get(channel, set())
        existed = str(sender_id) in users
        users.discard(str(sender_id))
        _save(data)
        return existed


def get_approved(channel: str) -> list[str]:
    with _LOCK:
        data = _load()
        return sorted(data.get("approved", {}).get(channel, set()))


def format_pairing_reply(code: str) -> str:
    return (
        "Hi there! This assistant only responds to approved users.\n\n"
        f"Your pairing code is: `{code}`\n\n"
        "Ask the owner to approve this code."
    )
