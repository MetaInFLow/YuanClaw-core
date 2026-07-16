"""Configuration loading utilities."""

import json
import os
from pathlib import Path
from typing import Any, Mapping

from yuanclaw.config.schema import Config
from yuanclaw.utils.atomic import atomic_write_text, backup_path, quarantine_path

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


class ConfigLoadError(ValueError):
    """Raised when a persisted configuration and its backup cannot be validated."""


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".yuanclaw" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        loaded: Config | None = None
        try:
            loaded = _load_config_file(path)
        except (json.JSONDecodeError, ValueError, TypeError) as primary_error:
            previous_path = backup_path(path)
            if previous_path.exists():
                try:
                    recovered = _load_config_file(previous_path)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                else:
                    quarantine_path(path)
                    atomic_write_text(
                        path,
                        json.dumps(
                            recovered.model_dump(by_alias=True),
                            indent=2,
                            ensure_ascii=False,
                        ),
                        keep_backup=False,
                    )
                    loaded = recovered
            if loaded is None:
                raise ConfigLoadError(f"Invalid configuration file: {path}") from primary_error
        if loaded is None:
            raise ConfigLoadError(f"Invalid configuration file: {path}")
        return _apply_environment_overrides(loaded)

    return _apply_environment_overrides(Config.model_validate({}))


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False),
    )


def _load_config_file(path: Path) -> Config:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise TypeError("configuration root must be an object")
    return Config.model_validate(_migrate_config(data))


def _apply_environment_overrides(
    config: Config,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Apply legacy then canonical nested environment overrides to file config."""
    merged = config.model_dump()
    source = os.environ if environ is None else environ
    for prefix in ("NANOBOT_", "YUANCLAW_"):
        for key, raw_value in source.items():
            if not key.upper().startswith(prefix):
                continue
            relative = key[len(prefix):]
            if "__" not in relative:
                continue
            path = [segment.strip().lower() for segment in relative.split("__")]
            if not path or any(not segment for segment in path):
                continue
            _set_nested_value(merged, path, _parse_environment_value(raw_value))
    return Config.model_validate(merged)


def _parse_environment_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _set_nested_value(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for segment in path[:-1]:
        child = cursor.get(segment)
        if not isinstance(child, dict):
            child = {}
            cursor[segment] = child
        cursor = child
    cursor[path[-1]] = value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
