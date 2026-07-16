"""Small JSON pairing store for DM sender approval."""

from __future__ import annotations

import json
import secrets
import string
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock

from yuanclaw.config.paths import get_data_dir
from yuanclaw.utils.atomic import atomic_write_text, backup_path, quarantine_path

_LOCK = threading.Lock()
_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 8


class PairingStoreCorruptError(ValueError):
    """Raised when pairing state and its backup cannot be read safely."""


def _store_path() -> Path:
    return get_data_dir() / "pairing.json"


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("pairing store root must be an object")
    approved = data.get("approved", {})
    pending = data.get("pending", {})
    if not isinstance(approved, dict) or not isinstance(pending, dict):
        raise TypeError("pairing store approved and pending fields must be objects")
    normalized_approved: dict[str, set[str]] = {}
    for channel, users in approved.items():
        if not isinstance(users, list):
            raise TypeError("pairing approved users must be arrays")
        normalized_approved[str(channel)] = {str(user) for user in users}
    if any(not isinstance(info, dict) for info in pending.values()):
        raise TypeError("pairing pending entries must be objects")
    return {"approved": normalized_approved, "pending": dict(pending)}


def _load() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"approved": {}, "pending": {}}
    try:
        return _read(path)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as primary_error:
        previous = backup_path(path)
        if previous.exists():
            try:
                recovered = _read(previous)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
            else:
                quarantine_path(path)
                _save(recovered, keep_backup=False)
                return recovered
        raise PairingStoreCorruptError(f"Invalid pairing store: {path}") from primary_error


def _save(data: dict[str, Any], *, keep_backup: bool = True) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "approved": {
            channel: sorted(users)
            for channel, users in data.get("approved", {}).items()
        },
        "pending": dict(data.get("pending", {})),
    }
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False),
        keep_backup=keep_backup,
    )


@contextmanager
def _locked_store():
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, FileLock(str(path.with_name(f"{path.name}.lock")), timeout=10):
        yield


def _gc_pending(data: dict[str, Any]) -> None:
    now = time.time()
    pending = data.get("pending", {})
    for code, info in list(pending.items()):
        if float(info.get("expires_at", 0) or 0) <= now:
            pending.pop(code, None)


def generate_code(channel: str, sender_id: str, ttl: int = 600) -> str:
    with _locked_store():
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
    with _locked_store():
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
    with _locked_store():
        data = _load()
        existed = code in data.get("pending", {})
        data.get("pending", {}).pop(code, None)
        _save(data)
        return existed


def is_approved(channel: str, sender_id: str) -> bool:
    with _locked_store():
        data = _load()
        return str(sender_id) in data.get("approved", {}).get(channel, set())


def list_pending() -> list[dict[str, Any]]:
    with _locked_store():
        data = _load()
        _gc_pending(data)
        _save(data)
        return [{"code": code, **info} for code, info in data.get("pending", {}).items()]


def revoke(channel: str, sender_id: str) -> bool:
    with _locked_store():
        data = _load()
        users = data.get("approved", {}).get(channel, set())
        existed = str(sender_id) in users
        users.discard(str(sender_id))
        _save(data)
        return existed


def get_approved(channel: str) -> list[str]:
    with _locked_store():
        data = _load()
        return sorted(data.get("approved", {}).get(channel, set()))


def format_pairing_reply(code: str) -> str:
    return (
        "Hi there! This assistant only responds to approved users.\n\n"
        f"Your pairing code is: `{code}`\n\n"
        "Ask the owner to approve this code."
    )
