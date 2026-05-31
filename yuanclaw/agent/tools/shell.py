"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.agent.tools.exec_session import (
    DEFAULT_EXEC_SESSION_MANAGER,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_YIELD_MS,
    MAX_OUTPUT_CHARS,
    MAX_YIELD_MS,
    ExecSessionManager,
    clamp_session_int,
    format_session_poll,
)
from yuanclaw.security.workspace_access import current_tool_workspace
from yuanclaw.security.workspace_policy import WORKSPACE_BOUNDARY_NOTE, is_path_within

SAFE_ENV_KEYS = {
    "COLORTERM",
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}
SAFE_ENV_PREFIXES = ("LC_",)


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        session_manager: ExecSessionManager | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._session_manager = session_manager or DEFAULT_EXEC_SESSION_MANAGER
        self._owner_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._owner_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                },
                "yield_time_ms": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": MAX_YIELD_MS,
                    "description": "Optional milliseconds to wait before returning a pollable session_id.",
                },
                "max_output_chars": {
                    "type": ["integer", "null"],
                    "minimum": 1000,
                    "maximum": MAX_OUTPUT_CHARS,
                    "description": "Maximum output characters to return.",
                },
                "max_output_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1000,
                    "maximum": MAX_OUTPUT_CHARS,
                    "description": "Compatibility alias for max_output_chars.",
                },
            },
            "required": ["command"]
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        max_output_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        access = current_tool_workspace(
            self.working_dir,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        workspace_root = str(access.project_path) if access.project_path is not None else self.working_dir
        cwd = self._resolve_cwd(working_dir, workspace_root)
        if access.restrict_to_workspace and workspace_root:
            try:
                requested = Path(cwd).expanduser().resolve()
                resolved_root = Path(workspace_root).expanduser().resolve()
            except Exception:
                return "Error: working_dir could not be resolved" + WORKSPACE_BOUNDARY_NOTE
            if not is_path_within(requested, resolved_root):
                return (
                    "Error: working_dir is outside the configured workspace"
                    + WORKSPACE_BOUNDARY_NOTE
                )
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        env = self._build_env()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            if max_output_chars is None:
                max_output_chars = max_output_tokens
            output_limit = clamp_session_int(
                max_output_chars, DEFAULT_MAX_OUTPUT_CHARS, 1000, MAX_OUTPUT_CHARS
            )
            if yield_time_ms is not None:
                session_id, poll = await self._session_manager.start(
                    owner_key=self._owner_key,
                    command=command,
                    cwd=cwd,
                    env=env,
                    timeout=self.timeout,
                    yield_time_ms=clamp_session_int(
                        yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS
                    ),
                    max_output_chars=output_limit,
                )
                return format_session_poll(session_id, poll)

            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                # Wait for the process to fully terminate so pipes are
                # drained and file descriptors are released.
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {self.timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = output_limit
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    @staticmethod
    def _resolve_cwd(working_dir: str | None, workspace_root: str | None) -> str:
        base = workspace_root or os.getcwd()
        if not working_dir:
            return base
        candidate = Path(working_dir).expanduser()
        if not candidate.is_absolute():
            candidate = Path(base) / candidate
        return str(candidate)

    @staticmethod
    def _build_env() -> dict[str, str]:
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in SAFE_ENV_KEYS or key.startswith(SAFE_ENV_PREFIXES):
                env[key] = value
        return env

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", command) # POSIX: /absolute only
        return win_paths + posix_paths
