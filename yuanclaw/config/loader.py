"""Configuration loading utilities."""

import json
from pathlib import Path

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
        try:
            return _load_config_file(path)
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
                    return recovered
            raise ConfigLoadError(f"Invalid configuration file: {path}") from primary_error

    return Config()


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


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
