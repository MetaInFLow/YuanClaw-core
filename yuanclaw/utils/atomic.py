"""Crash-resistant local file persistence helpers."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

_WRITE_LOCKS: dict[str, threading.RLock] = {}


def backup_path(path: Path) -> Path:
    """Return the stable previous-version path for a persisted file."""
    return path.with_name(f"{path.name}.bak")


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    keep_backup: bool = True,
) -> None:
    """Write text by fsyncing a sibling temporary file and atomically replacing the target."""
    lock = _WRITE_LOCKS.setdefault(str(path.expanduser().resolve()), threading.RLock())
    with lock:
        _atomic_write_text_unlocked(
            path,
            content,
            encoding=encoding,
            keep_backup=keep_backup,
        )


def _atomic_write_text_unlocked(
    path: Path,
    content: str,
    *,
    encoding: str,
    keep_backup: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if keep_backup and path.exists():
            previous_path = backup_path(path)
            backup_temp = temp_path.with_suffix(".bak.tmp")
            try:
                shutil.copyfile(path, backup_temp)
                with open(backup_temp, "r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(backup_temp, previous_path)
                _sync_directory(path.parent)
            finally:
                backup_temp.unlink(missing_ok=True)

        os.replace(temp_path, path)
        _sync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def quarantine_path(path: Path) -> Path:
    """Move a damaged file aside without replacing an earlier quarantine copy."""
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.corrupt-{index}")
        if not candidate.exists():
            os.replace(path, candidate)
            return candidate
        index += 1
