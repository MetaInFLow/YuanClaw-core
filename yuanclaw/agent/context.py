"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from yuanclaw.agent.memory import MemoryStore
from yuanclaw.agent.skills import SkillsLoader
from yuanclaw.apps.cli import cli_app_runtime_lines
from yuanclaw.apps.mcp_presets import mcp_preset_runtime_lines
from yuanclaw.security.workspace_access import current_tool_workspace
from yuanclaw.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from yuanclaw.session.goal_state import goal_state_runtime_lines
from yuanclaw.utils.helpers import build_assistant_message, current_time_str, detect_image_mime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _MAX_ACTIVE_SKILLS = 16
    _MAX_SKILL_CONTEXT_CHARS = 24_000
    _MAX_SKILL_CHARS = 12_000
    _MAX_IMAGES = 4
    _MAX_IMAGE_BYTES = 10 * 1024 * 1024
    _MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        memory_context: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context() if memory_context is None else memory_context
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        active_skills = list(dict.fromkeys([
            *self.skills.get_always_skills(),
            *(name for name in (skill_names or []) if isinstance(name, str)),
        ]))[:self._MAX_ACTIVE_SKILLS]
        if active_skills:
            always_content = self.skills.load_skills_for_context(
                active_skills,
                max_total_chars=self._MAX_SKILL_CONTEXT_CHARS,
                max_skill_chars=self._MAX_SKILL_CHARS,
            )
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return f"""# yuanclaw 🐈

You are yuanclaw, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md (write important facts here)
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## yuanclaw Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        session_metadata: dict[str, Any] | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str()}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_summary:
            lines.extend(["", session_summary])
        lines.extend(goal_state_runtime_lines(session_metadata))
        lines.extend(cli_app_runtime_lines(session_metadata))
        lines.extend(mcp_preset_runtime_lines(session_metadata))
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        memory_context: str | None = None,
        session_metadata: dict[str, Any] | None = None,
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id, session_metadata, session_summary)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        return [
            {"role": "system", "content": self.build_system_prompt(skill_names, memory_context)},
            *history,
            {"role": "user", "content": merged},
        ]

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        total_image_bytes = 0
        for path in media:
            if len(images) >= self._MAX_IMAGES:
                break
            try:
                tool_workspace = current_tool_workspace(self.workspace)
                p = resolve_allowed_path(
                    path,
                    workspace=tool_workspace.project_path or self.workspace,
                    allowed_root=tool_workspace.allowed_root,
                )
            except WorkspaceBoundaryError:
                continue
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if (
                size > self._MAX_IMAGE_BYTES
                or total_image_bytes + size > self._MAX_TOTAL_IMAGE_BYTES
            ):
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            total_image_bytes += len(raw)

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
        usage: dict[str, int] | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(
            build_assistant_message(
                content,
                tool_calls=tool_calls,
                reasoning_content=reasoning_content,
                thinking_blocks=thinking_blocks,
                usage=usage,
                model=model,
                provider=provider,
            )
        )
        return messages
