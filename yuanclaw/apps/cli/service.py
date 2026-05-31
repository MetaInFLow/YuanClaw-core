"""Core CLI Apps catalog and install-state service."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from yuanclaw.apps.cli.utils import _safe_name


class CliAppServiceError(ValueError):
    """User-facing CLI Apps service failure."""


def _now() -> int:
    return int(time.time())


def _read_list(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _write_list(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, indent=2, ensure_ascii=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_now()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _normalize_app(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    name = _safe_name(raw.get("name"))
    if not name:
        return None
    entry_point = str(raw.get("entry_point") or raw.get("entryPoint") or "").strip()
    display_name = str(raw.get("display_name") or raw.get("displayName") or "").strip()
    description = str(raw.get("description") or "").strip()
    settings = raw.get("settings")

    app: dict[str, Any] = {"name": name}
    if display_name:
        app["display_name"] = display_name
    if entry_point:
        app["entry_point"] = entry_point
    if description:
        app["description"] = description
    if isinstance(settings, dict):
        app["settings"] = dict(settings)
    return app


class CliAppService:
    """Manage local CLI app catalog and installed app state."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / "apps" / "cli"
        self.catalog_path = self.root / "catalog.json"
        self.installed_path = self.root / "installed.json"

    def catalog(self) -> list[dict[str, Any]]:
        return [
            normalized
            for raw in _read_list(self.catalog_path)
            if (normalized := _normalize_app(raw)) is not None
        ]

    def replace_catalog(self, apps: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in apps:
            normalized = _normalize_app(raw)
            if normalized is None or normalized["name"] in seen:
                continue
            rows.append(normalized)
            seen.add(normalized["name"])
        _write_list(self.catalog_path, rows)
        return rows

    def installed(self) -> list[dict[str, Any]]:
        return [
            normalized
            for raw in _read_list(self.installed_path)
            if (normalized := _normalize_app(raw)) is not None
        ]

    def list_catalog(self) -> list[dict[str, Any]]:
        installed = {item["name"]: item for item in self.installed()}
        rows: list[dict[str, Any]] = []
        for app in self.catalog():
            installed_app = installed.get(app["name"])
            row = dict(app)
            row["installed"] = installed_app is not None
            if installed_app and isinstance(installed_app.get("settings"), dict):
                row["settings"] = dict(installed_app["settings"])
            rows.append(row)
        return rows

    def install(self, name: str, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        app = self._catalog_app(name)
        if not app.get("entry_point"):
            raise CliAppServiceError(f"CLI app '{name}' has no entry point")
        row = dict(app)
        row["installed_at"] = _now()
        row["updated_at"] = row["installed_at"]
        if settings is not None:
            row["settings"] = dict(settings)
        self._upsert_installed(row)
        return row

    def update_settings(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        installed = self.installed()
        target = _safe_name(name)
        for index, app in enumerate(installed):
            if app["name"] == target:
                updated = dict(app)
                updated["settings"] = dict(settings)
                updated["updated_at"] = _now()
                installed[index] = updated
                _write_list(self.installed_path, installed)
                return updated
        raise CliAppServiceError(f"CLI app '{name}' is not installed")

    def uninstall(self, name: str) -> bool:
        target = _safe_name(name)
        installed = self.installed()
        kept = [app for app in installed if app["name"] != target]
        _write_list(self.installed_path, kept)
        return len(kept) != len(installed)

    def test_installed(self, name: str) -> dict[str, Any]:
        app = self._installed_app(name)
        return self.test_entry_point(str(app.get("entry_point") or ""))

    @staticmethod
    def test_entry_point(entry_point: str) -> dict[str, Any]:
        entry_point = entry_point.strip()
        executable = shutil.which(entry_point) if entry_point else None
        return {
            "ok": executable is not None,
            "entry_point": entry_point,
            "path": executable,
            "message": "entry point found" if executable else "entry point not found on PATH",
        }

    def _catalog_app(self, name: str) -> dict[str, Any]:
        target = _safe_name(name)
        for app in self.catalog():
            if app["name"] == target:
                return app
        raise CliAppServiceError(f"CLI app '{name}' is not in the catalog")

    def _installed_app(self, name: str) -> dict[str, Any]:
        target = _safe_name(name)
        for app in self.installed():
            if app["name"] == target:
                return app
        raise CliAppServiceError(f"CLI app '{name}' is not installed")

    def _upsert_installed(self, row: dict[str, Any]) -> None:
        installed = self.installed()
        for index, app in enumerate(installed):
            if app["name"] == row["name"]:
                installed[index] = row
                _write_list(self.installed_path, installed)
                return
        installed.append(row)
        _write_list(self.installed_path, installed)
