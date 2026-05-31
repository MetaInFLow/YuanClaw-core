"""Tool for running installed CLI Apps."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.security.workspace_access import current_tool_workspace
from yuanclaw.security.workspace_policy import WORKSPACE_BOUNDARY_NOTE, is_path_within


class CliAppError(ValueError):
    pass


class CliAppManager:
    """Read local CLI app install state and execute app entry points."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.installed_path = workspace / "apps" / "cli" / "installed.json"

    def installed(self) -> list[dict[str, Any]]:
        if not self.installed_path.exists():
            return []
        try:
            raw = json.loads(self.installed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def installed_names(self) -> list[str]:
        return [
            str(item.get("name") or "").strip().lower()
            for item in self.installed()
            if str(item.get("name") or "").strip()
        ]

    def get_installed(self, name: str) -> dict[str, Any]:
        target = name.strip().lower()
        for item in self.installed():
            if str(item.get("name") or "").strip().lower() == target:
                return item
        raise CliAppError(f"CLI app '{name}' is not installed")

    def mentioned_installed_apps(self, text: str) -> list[dict[str, str]]:
        lowered = f" {text.lower()} "
        matches: list[dict[str, str]] = []
        for item in self.installed():
            name = str(item.get("name") or "").strip().lower()
            if not name or f"@{name}" not in lowered:
                continue
            matches.append(
                {
                    "name": name,
                    "tool": "run_cli_app",
                    "entry_point": str(item.get("entry_point") or "").strip(),
                    "skill": f"skills/cli-app-{name}/SKILL.md",
                }
            )
        return matches


class CliAppsTool(Tool):
    def __init__(
        self,
        *,
        workspace: Path,
        timeout: int = 60,
        restrict_to_workspace: bool = False,
    ) -> None:
        self.workspace = workspace
        self.timeout = timeout
        self.restrict_to_workspace = restrict_to_workspace

    @property
    def name(self) -> str:
        return "run_cli_app"

    @property
    def description(self) -> str:
        return (
            "Run an installed CLI App by registry name. Use this for attached CLI Apps; "
            "do not call arbitrary shell commands for app integrations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "working_dir": {"type": ["string", "null"]},
                "json": {"type": "boolean"},
                "timeout": {"type": ["integer", "null"], "minimum": 1, "maximum": 600},
            },
            "required": ["name"],
        }

    async def execute(
        self,
        name: str,
        args: list[str] | None = None,
        working_dir: str | None = None,
        json: bool = False,
        timeout: int | None = None,
        **_kwargs: Any,
    ) -> str:
        try:
            manager = CliAppManager(self.workspace)
            app = manager.get_installed(name)
            entry_point = str(app.get("entry_point") or app.get("entryPoint") or "").strip()
            if not entry_point:
                raise CliAppError(f"CLI app '{name}' has no entry point")
            cwd = self._resolve_cwd(working_dir)
            executable = shutil.which(entry_point)
            if executable is None:
                raise CliAppError(f"{entry_point} is not available on PATH")

            argv = [executable]
            if json:
                argv.append("--json")
            argv.extend(str(item) for item in (args or []))

            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(max(int(timeout or self.timeout), 1), 600),
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return f"Error: CLI app timed out after {timeout or self.timeout} seconds"

            parts = []
            if stdout:
                parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                parts.append("STDERR:\n" + stderr.decode("utf-8", errors="replace"))
            parts.append(f"Exit code: {process.returncode}")
            result = "\n".join(parts)
            return result[:12_000] + ("\n... (truncated)" if len(result) > 12_000 else "")
        except CliAppError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error running CLI app: {exc}"

    def _resolve_cwd(self, working_dir: str | None) -> Path:
        access = current_tool_workspace(
            self.workspace,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        workspace_root = access.project_path or self.workspace
        cwd = Path(working_dir).expanduser() if working_dir else workspace_root
        if not cwd.is_absolute():
            cwd = workspace_root / cwd
        resolved = cwd.resolve()
        if access.restrict_to_workspace:
            root = workspace_root.resolve()
            if not is_path_within(resolved, root):
                raise CliAppError(
                    "working_dir is outside the configured workspace"
                    + WORKSPACE_BOUNDARY_NOTE
                )
        return resolved
