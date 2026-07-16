"""Session management for conversation history."""

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from yuanclaw.config.paths import get_legacy_sessions_dir
from yuanclaw.utils.atomic import atomic_write_text, backup_path, quarantine_path
from yuanclaw.utils.helpers import ensure_dir, safe_filename


class SessionCorruptError(ValueError):
    """Raised when a persisted session and its backup cannot be read safely."""


def _usage_bucket() -> dict[str, Any]:
    """Create an empty usage bucket."""
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "last_used_at": None,
    }


def _normalize_usage_values(usage: dict[str, Any] | None) -> dict[str, int]:
    """Normalize usage counters to non-negative integers."""
    usage = usage or {}
    prompt_tokens = max(0, int(usage.get("prompt_tokens", 0) or 0))
    completion_tokens = max(0, int(usage.get("completion_tokens", 0) or 0))
    total_tokens = max(
        0,
        int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0),
    )
    requests = max(0, int(usage.get("requests", 0) or 0))
    return {
        "requests": requests,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _merge_usage_bucket(target: dict[str, Any], source: dict[str, Any] | None) -> None:
    """Merge usage counters into a bucket in place."""
    normalized = _normalize_usage_values(source)
    for key, value in normalized.items():
        target[key] = int(target.get(key, 0) or 0) + value

    source_last_used_at = (source or {}).get("last_used_at")
    if source_last_used_at:
        current_last_used_at = target.get("last_used_at")
        if not current_last_used_at or str(source_last_used_at) > str(current_last_used_at):
            target["last_used_at"] = source_last_used_at


def _assistant_usage_snapshot(message: dict[str, Any]) -> dict[str, Any] | None:
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    provider = str(message.get("provider") or "").strip()
    model = str(message.get("model") or "").strip()
    has_request_marker = bool(usage) or bool(provider or model)
    if not has_request_marker:
        return None

    normalized = _normalize_usage_values(
        {
            "requests": usage.get("requests", 1),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    )
    return {
        **normalized,
        "provider": provider or "unknown",
        "model": model or "unknown",
        "last_used_at": message.get("timestamp"),
    }


def _attachment_breadcrumbs(message: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    cli_apps = message.get("cli_apps")
    if isinstance(cli_apps, list):
        for item in cli_apps[:8]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if not name:
                continue
            entry_point = str(item.get("entry_point") or "unknown").strip() or "unknown"
            lines.append(
                "CLI App Attachment: "
                f"@{name} (tool=run_cli_app; entry_point={entry_point})."
            )

    mcp_presets = message.get("mcp_presets")
    if isinstance(mcp_presets, list):
        for item in mcp_presets[:8]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if not name:
                continue
            transport = str(item.get("transport") or "mcp").strip() or "mcp"
            lines.append(
                "MCP Preset Attachment: "
                f"@{name} (transport={transport}; tool_prefix=mcp_{name}_)."
            )
    return lines


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            if m.get("role") == "user" and isinstance(entry["content"], str):
                breadcrumbs = _attachment_breadcrumbs(m)
                if breadcrumbs:
                    entry["content"] = "\n".join(breadcrumbs + ["", entry["content"]])
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def get_consolidation_messages(
        self,
        *,
        archive_all: bool = False,
        keep_count: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the message slice used for memory consolidation.

        This is a pure helper and does not modify the JSONL persistence shape.
        """
        if archive_all:
            return list(self.messages)
        if keep_count <= 0:
            return self.messages[self.last_consolidated:]
        return self.messages[self.last_consolidated:-keep_count]

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}
        self._cache_mtime_ns: dict[str, int] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))[:80] or "session"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        return self.sessions_dir / f"{safe_key}-{digest}.jsonl"

    def _get_old_workspace_session_path(self, key: str) -> Path:
        """Return the pre-0.2.0 workspace filename used before keys were hashed."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.yuanclaw/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        cached = self._cached_session(key)
        if cached is not None:
            return cached

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        path = self._get_session_path(key)
        if path.exists():
            self._cache_mtime_ns[key] = path.stat().st_mtime_ns
        return session

    def _cached_session(self, key: str) -> Session | None:
        session = self._cache.get(key)
        if session is None:
            return None
        persisted_mtime = self._cache_mtime_ns.get(key)
        if persisted_mtime is None:
            return session

        path = self._get_session_path(key)
        try:
            current_mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            self.invalidate(key)
            return None
        if current_mtime == persisted_mtime:
            return session

        self.invalidate(key)
        return None

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            for legacy_path in (
                self._get_old_workspace_session_path(key),
                self._get_legacy_session_path(key),
            ):
                if not legacy_path.exists() or not self._path_belongs_to_key(legacy_path, key):
                    continue
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                    break
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            return self._read_session(path, key)
        except Exception as primary_error:
            previous_path = backup_path(path)
            if previous_path.exists():
                try:
                    recovered = self._read_session(previous_path, key)
                except Exception:
                    pass
                else:
                    quarantine_path(path)
                    atomic_write_text(
                        path,
                        self._serialize(recovered),
                        keep_backup=False,
                    )
                    return recovered
            raise SessionCorruptError(f"Invalid session file for {key}: {path}") from primary_error

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        atomic_write_text(path, self._serialize(session))

        self._cache[session.key] = session
        self._cache_mtime_ns[session.key] = path.stat().st_mtime_ns

    @staticmethod
    def _serialize(session: Session) -> str:
        metadata_line = {
            "_type": "metadata",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "last_consolidated": max(0, min(session.last_consolidated, len(session.messages))),
        }
        lines = [json.dumps(metadata_line, ensure_ascii=False)]
        lines.extend(json.dumps(message, ensure_ascii=False) for message in session.messages)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _path_belongs_to_key(path: Path, key: str) -> bool:
        try:
            with open(path, encoding="utf-8") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    data = json.loads(raw_line)
                    return data.get("_type") == "metadata" and data.get("key") == key
        except Exception:
            return False
        return False

    @staticmethod
    def _read_session(path: Path, key: str) -> Session:
        messages: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        created_at: datetime | None = None
        updated_at: datetime | None = None
        last_consolidated = 0
        stored_key: str | None = None

        with open(path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise TypeError("session record must be an object")
                if data.get("_type") == "metadata":
                    stored_key = data.get("key")
                    metadata = data.get("metadata", {})
                    if not isinstance(metadata, dict):
                        raise TypeError("session metadata must be an object")
                    created_at = (
                        datetime.fromisoformat(data["created_at"])
                        if data.get("created_at")
                        else None
                    )
                    updated_at = (
                        datetime.fromisoformat(data["updated_at"])
                        if data.get("updated_at")
                        else None
                    )
                    last_consolidated = int(data.get("last_consolidated", 0) or 0)
                else:
                    messages.append(data)

        if stored_key != key:
            raise ValueError(f"session key mismatch: expected {key!r}, found {stored_key!r}")
        last_consolidated = max(0, min(last_consolidated, len(messages)))
        return Session(
            key=key,
            messages=messages,
            created_at=created_at or datetime.now(),
            updated_at=updated_at or created_at or datetime.now(),
            metadata=metadata,
            last_consolidated=last_consolidated,
        )

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Read a session without creating it when missing."""
        session = self._cached_session(key)
        if session is not None:
            return {
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": dict(session.metadata),
                "last_consolidated": session.last_consolidated,
                "messages": [dict(message) for message in session.messages],
            }

        loaded = self._load(key)
        if loaded is None:
            return None
        self._cache[key] = loaded
        path = self._get_session_path(key)
        self._cache_mtime_ns[key] = path.stat().st_mtime_ns
        return {
            "key": loaded.key,
            "created_at": loaded.created_at.isoformat(),
            "updated_at": loaded.updated_at.isoformat(),
            "metadata": dict(loaded.metadata),
            "last_consolidated": loaded.last_consolidated,
            "messages": [dict(message) for message in loaded.messages],
        }

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
        self._cache_mtime_ns.pop(key, None)

    def delete(self, key: str) -> bool:
        """Delete a persisted session and evict every cached copy."""
        self.invalidate(key)
        removed = False
        current_path = self._get_session_path(key)
        candidates = (
            current_path,
            backup_path(current_path),
            self._get_old_workspace_session_path(key),
            self._get_legacy_session_path(key),
        )
        for path in candidates:
            if not path.exists():
                continue
            if path != current_path and path != backup_path(current_path):
                if not self._path_belongs_to_key(path, key):
                    continue
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
        return removed

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                metadata: dict[str, Any] | None = None
                message_count = 0
                last_message: dict[str, Any] | None = None

                with open(path, encoding="utf-8") as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line:
                            continue

                        data = json.loads(line)
                        if data.get("_type") == "metadata":
                            metadata = data
                            continue

                        message_count += 1
                        last_message = data

                if metadata is None:
                    continue

                key = metadata.get("key") or path.stem.replace("_", ":", 1)
                sessions.append({
                    "key": key,
                    "created_at": metadata.get("created_at"),
                    "updated_at": metadata.get("updated_at"),
                    "path": str(path),
                    "message_count": message_count,
                    "last_role": last_message.get("role") if last_message else None,
                    "last_message_preview": self._preview_message(last_message),
                    "thread_summary": (metadata.get("metadata") or {}).get("thread_summary"),
                })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    @staticmethod
    def _preview_message(message: dict[str, Any] | None, limit: int = 120) -> str | None:
        """Build a compact preview for list APIs."""
        if not message:
            return None

        content = str(message.get("content") or "").strip()
        if not content:
            return None

        normalized = " ".join(content.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    def set_thread_summary(self, key: str, summary: str) -> Session:
        """Persist a UI-facing thread summary in session metadata."""
        session = self.get_or_create(key)
        session.metadata["thread_summary"] = summary
        session.updated_at = datetime.now()
        self.save(session)
        return session

    def record_usage(
        self,
        session_or_key: Session | str,
        *,
        provider: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> Session:
        """Accumulate LLM usage counters in session metadata."""
        session = (
            session_or_key
            if isinstance(session_or_key, Session)
            else self.get_or_create(session_or_key)
        )
        usage_snapshot = _normalize_usage_values(usage)
        if not any(usage_snapshot.values()):
            return session

        now = datetime.now().isoformat()
        usage_meta = session.metadata.setdefault("usage", _usage_bucket())
        _merge_usage_bucket(usage_meta, {**usage_snapshot, "last_used_at": now})

        if provider:
            providers = usage_meta.setdefault("providers", {})
            provider_bucket = providers.setdefault(provider, _usage_bucket())
            _merge_usage_bucket(provider_bucket, {**usage_snapshot, "last_used_at": now})

        if model:
            models = usage_meta.setdefault("models", {})
            model_bucket = models.setdefault(model, _usage_bucket())
            _merge_usage_bucket(model_bucket, {**usage_snapshot, "last_used_at": now})

        session.updated_at = datetime.now()
        return session

    def summarize_usage(self) -> dict[str, Any]:
        """Aggregate persisted usage counters across all sessions."""
        totals = _usage_bucket()
        providers: dict[str, dict[str, Any]] = {}
        models: dict[str, dict[str, Any]] = {}
        sessions_with_usage = 0

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                metadata: dict[str, Any] | None = None
                assistant_messages: list[dict[str, Any]] = []

                with open(path, encoding="utf-8") as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line:
                            continue

                        data = json.loads(line)
                        if data.get("_type") == "metadata":
                            metadata = data
                            continue
                        if data.get("role") == "assistant":
                            assistant_messages.append(data)

                if metadata is None:
                    continue

                usage = ((metadata.get("metadata") or {}).get("usage") or {})
                normalized = _normalize_usage_values(usage)
                if not any(normalized.values()):
                    session_has_fallback_usage = False
                    for message in assistant_messages:
                        snapshot = _assistant_usage_snapshot(message)
                        if snapshot is None:
                            continue

                        if not session_has_fallback_usage:
                            sessions_with_usage += 1
                            session_has_fallback_usage = True

                        _merge_usage_bucket(totals, snapshot)

                        provider_bucket = providers.setdefault(
                            snapshot["provider"],
                            _usage_bucket(),
                        )
                        _merge_usage_bucket(provider_bucket, snapshot)

                        model_bucket = models.setdefault(
                            snapshot["model"],
                            _usage_bucket(),
                        )
                        _merge_usage_bucket(model_bucket, snapshot)
                    continue

                sessions_with_usage += 1
                _merge_usage_bucket(
                    totals,
                    {**normalized, "last_used_at": usage.get("last_used_at")},
                )

                for key, bucket in (usage.get("providers") or {}).items():
                    provider_bucket = providers.setdefault(key, _usage_bucket())
                    _merge_usage_bucket(provider_bucket, bucket)

                for key, bucket in (usage.get("models") or {}).items():
                    model_bucket = models.setdefault(key, _usage_bucket())
                    _merge_usage_bucket(model_bucket, bucket)
            except Exception:
                continue

        def _rows(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            rows = [
                {
                    "key": key,
                    "label": key,
                    **bucket,
                }
                for key, bucket in items.items()
            ]
            rows.sort(
                key=lambda item: (
                    -int(item.get("total_tokens", 0) or 0),
                    -int(item.get("requests", 0) or 0),
                    str(item.get("label") or ""),
                )
            )
            return rows

        return {
            "available": sessions_with_usage > 0,
            "sessions": sessions_with_usage,
            "last_used_at": totals.get("last_used_at"),
            "totals": totals,
            "providers": _rows(providers),
            "models": _rows(models),
        }
