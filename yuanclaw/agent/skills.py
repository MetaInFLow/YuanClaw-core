"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _truncate_with_marker(text: str, limit: int, marker: str) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    return text[:limit - len(marker)].rstrip() + marker


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    _VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in sorted(self.workspace_skills.iterdir(), key=lambda path: path.name.lower()):
                if skill_dir.is_dir():
                    skill_file = self._skill_file(self.workspace_skills, skill_dir.name)
                    if skill_file is not None:
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in sorted(self.builtin_skills.iterdir(), key=lambda path: path.name.lower()):
                if skill_dir.is_dir():
                    skill_file = self._skill_file(self.builtin_skills, skill_dir.name)
                    if skill_file is not None and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        workspace_skill = self._skill_file(self.workspace_skills, name)
        if workspace_skill is not None:
            return workspace_skill.read_text(encoding="utf-8")

        if self.builtin_skills:
            builtin_skill = self._skill_file(self.builtin_skills, name)
            if builtin_skill is not None:
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(
        self,
        skill_names: list[str],
        *,
        max_total_chars: int = 24_000,
        max_skill_chars: int = 12_000,
    ) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        used_chars = 0
        seen: set[str] = set()
        available = {
            item["name"]
            for item in self.list_skills(filter_unavailable=True)
        }
        for name in skill_names:
            if not isinstance(name, str):
                continue
            if name in seen or name not in available:
                continue
            seen.add(name)
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                content = _truncate_with_marker(
                    content,
                    max_skill_chars,
                    "\n\n... (skill truncated)",
                )
                section = f"### Skill: {name}\n\n{content}"
                separator_chars = len("\n\n---\n\n") if parts else 0
                remaining = max_total_chars - used_chars - separator_chars
                if remaining <= 0:
                    break
                if len(section) > remaining:
                    section = _truncate_with_marker(
                        section,
                        remaining,
                        "\n\n... (skills truncated)",
                    )
                parts.append(section)
                used_chars += separator_chars + len(section)

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = escape_xml(s["path"])
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        supported_os = self._supported_os(skill_meta)
        if supported_os and not self._matches_current_os(supported_os):
            missing.append(f"OS: {', '.join(supported_os)}")
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return str(meta["description"])
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\r?\n.*?\r?\n---(?:\r?\n|$)", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: Any) -> dict:
        """Parse skill metadata JSON from frontmatter (supports yuanclaw and openclaw keys)."""
        try:
            data = raw if isinstance(raw, dict) else json.loads(raw)
            return data.get("yuanclaw", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (platform, bins, env vars)."""
        supported_os = self._supported_os(skill_meta)
        if supported_os and not self._matches_current_os(supported_os):
            return False
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    @staticmethod
    def _supported_os(skill_meta: dict) -> list[str]:
        raw = skill_meta.get("os", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [item.strip().lower() for item in raw if isinstance(item, str) and item.strip()]

    @staticmethod
    def _matches_current_os(supported_os: list[str]) -> bool:
        if sys.platform.startswith("win"):
            aliases = {"win", "win32", "windows"}
        elif sys.platform == "darwin":
            aliases = {"darwin", "mac", "macos", "osx"}
        elif sys.platform.startswith("linux"):
            aliases = {"linux"}
        else:
            aliases = {sys.platform.lower()}
        return bool(aliases.intersection(supported_os))

    def _get_skill_meta(self, name: str) -> dict:
        """Get yuanclaw metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.DOTALL)
            if match:
                try:
                    metadata = yaml.safe_load(match.group(1))
                except yaml.YAMLError:
                    return None
                return metadata if isinstance(metadata, dict) else None

        return None

    @classmethod
    def _skill_file(cls, root: Path, name: str) -> Path | None:
        if not cls._VALID_NAME.fullmatch(name) or name in {".", ".."}:
            return None
        root_resolved = root.resolve()
        candidate = (root / name / "SKILL.md").resolve()
        if candidate.parent.parent != root_resolved or not candidate.is_file():
            return None
        return candidate
