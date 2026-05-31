"""Apply structured file edits."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.agent.tools.filesystem import _resolve_path


@dataclass(slots=True)
class _PatchSummary:
    action: str
    path: str
    added: int = 0
    deleted: int = 0


class _PatchError(ValueError):
    pass


_ABSOLUTE_WINDOWS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _validate_relative_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        raise _PatchError("patch path cannot be empty")
    if "\0" in normalized:
        raise _PatchError(f"patch path contains a null byte: {path!r}")
    if normalized.startswith(("~", "/", "\\")) or _ABSOLUTE_WINDOWS_RE.match(normalized):
        raise _PatchError(f"patch path must be relative: {path}")
    if any(part == ".." for part in re.split(r"[\\/]+", normalized)):
        raise _PatchError(f"patch path must not contain '..': {path}")
    return normalized


def _text_line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _line_diff_stats(before: str, after: str) -> tuple[int, int]:
    before_lines = before.replace("\r\n", "\n").splitlines()
    after_lines = after.replace("\r\n", "\n").splitlines()
    added = 0
    deleted = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def _format_summary(summary: _PatchSummary) -> str:
    stats = ""
    if summary.added or summary.deleted:
        stats = f" (+{summary.added}/-{summary.deleted})"
    return f"- {summary.action} {summary.path}{stats}"


class ApplyPatchTool(Tool):
    """Apply multi-file edits with exact replace/add operations."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Default tool for code edits. Supports multi-file changes in a single call. "
            "Provide structured edits with relative paths. Set dry_run=true to validate "
            "and preview without writing files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "action": {"type": "string", "enum": ["replace", "add"]},
                            "old_text": {"type": ["string", "null"]},
                            "new_text": {"type": ["string", "null"]},
                        },
                        "required": ["path", "action"],
                    },
                    "minItems": 1,
                    "maxItems": 20,
                },
                "dry_run": {"type": "boolean"},
            },
            "required": ["edits"],
        }

    async def execute(
        self,
        edits: list[dict] | None = None,
        dry_run: bool = False,
        **_kwargs: Any,
    ) -> str:
        try:
            if not edits:
                raise _PatchError("must provide edits")

            writes: dict[Path, str] = {}
            summaries: list[_PatchSummary] = []

            for edit in edits:
                if not isinstance(edit, dict):
                    raise _PatchError("each edit must be an object")
                raw_path = edit.get("path")
                if not isinstance(raw_path, str):
                    raise _PatchError("path required for edit")
                path = _validate_relative_path(raw_path)
                action = edit.get("action")
                if not isinstance(action, str):
                    raise _PatchError(f"action required for edit: {path}")
                source = _resolve_path(path, self._workspace, self._allowed_dir)

                if action == "add":
                    summary = self._apply_add(edit, path, source, writes)
                elif action == "replace":
                    summary = self._apply_replace(edit, path, source, writes)
                else:
                    raise _PatchError(f"unknown action: {action}")
                summaries.append(summary)

            if dry_run:
                return "Patch dry-run succeeded:\n" + "\n".join(
                    _format_summary(summary) for summary in summaries
                )

            self._preflight_writes(writes)
            backups: dict[Path, tuple[bool, bytes]] = {}
            try:
                for path, content in writes.items():
                    if path not in backups:
                        backups[path] = (
                            path.exists(),
                            path.read_bytes() if path.exists() and path.is_file() else b"",
                        )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8", newline="")
            except (OSError, PermissionError):
                self._rollback_writes(backups)
                raise

            return "Patch applied:\n" + "\n".join(
                _format_summary(summary) for summary in summaries
            )
        except (OSError, PermissionError, _PatchError) as exc:
            return f"Error: {exc}"

    def _read_text(self, source: Path, path: str, writes: dict[Path, str]) -> tuple[str, bool]:
        pending = writes.get(source)
        if pending is not None:
            return pending, True
        if source.exists():
            if not source.is_file():
                raise _PatchError(f"path to update is not a file: {path}")
            try:
                with source.open("r", encoding="utf-8", newline="") as handle:
                    return handle.read(), True
            except UnicodeDecodeError as exc:
                raise _PatchError(f"file is not UTF-8 text: {path}") from exc
        return "", False

    def _apply_add(
        self,
        edit: dict[str, Any],
        path: str,
        source: Path,
        writes: dict[Path, str],
    ) -> _PatchSummary:
        new_text = edit.get("new_text")
        if new_text is None:
            raise _PatchError(f"new_text required for add: {path}")
        content, exists = self._read_text(source, path, writes)
        line_sep = "\r\n" if exists and "\r\n" in content else "\n"
        append_text = str(new_text).replace("\r\n", "\n").replace("\r", "\n")
        if line_sep != "\n":
            append_text = append_text.replace("\n", line_sep)
        new_norm = content + append_text
        if new_norm and not new_norm.endswith(line_sep):
            new_norm += line_sep
        writes[source] = new_norm
        if exists:
            added, deleted = _line_diff_stats(content, new_norm)
            return _PatchSummary("update", path, added, deleted)
        return _PatchSummary("add", path, _text_line_count(new_norm), 0)

    def _apply_replace(
        self,
        edit: dict[str, Any],
        path: str,
        source: Path,
        writes: dict[Path, str],
    ) -> _PatchSummary:
        old_text = edit.get("old_text") or ""
        if not old_text:
            raise _PatchError(f"old_text required for replace: {path}")
        new_text = edit.get("new_text")
        if new_text is None:
            raise _PatchError(f"new_text required for replace: {path}")

        content, exists = self._read_text(source, path, writes)
        if not exists:
            raise _PatchError(f"file to update does not exist: {path}")

        uses_crlf = "\r\n" in content
        norm_content = content.replace("\r\n", "\n")
        norm_old = str(old_text).replace("\r\n", "\n")
        pos = norm_content.find(norm_old)
        if pos < 0:
            raise _PatchError(f"old_text not found in {path}")
        if norm_content.find(norm_old, pos + 1) >= 0:
            raise _PatchError(f"old_text appears multiple times in {path}")

        new_norm = (
            norm_content[:pos]
            + str(new_text).replace("\r\n", "\n")
            + norm_content[pos + len(norm_old) :]
        )
        if new_norm and not new_norm.endswith("\n"):
            new_norm += "\n"
        if uses_crlf:
            new_norm = new_norm.replace("\n", "\r\n")
        writes[source] = new_norm
        added, deleted = _line_diff_stats(content, new_norm)
        return _PatchSummary("update", path, added, deleted)

    def _preflight_writes(self, writes: dict[Path, str]) -> None:
        for source in writes:
            existing_parent = source.parent
            while not existing_parent.exists() and existing_parent != existing_parent.parent:
                existing_parent = existing_parent.parent
            if existing_parent.exists() and not existing_parent.is_dir():
                raise _PatchError(f"parent path is not a directory: {existing_parent}")
            if source.exists() and not source.is_file():
                raise _PatchError(f"path to write is not a file: {source}")

    def _rollback_writes(self, backups: dict[Path, tuple[bool, bytes]]) -> None:
        for path, (existed, content) in reversed(list(backups.items())):
            try:
                if existed:
                    path.write_bytes(content)
                elif path.exists():
                    path.unlink()
            except OSError:
                pass
