from __future__ import annotations

import base64
import sys
from pathlib import Path

from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.skills import SkillsLoader


def _write_skill(
    workspace: Path,
    name: str,
    body: str,
    *,
    frontmatter: str = "",
) -> Path:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n{body}" if frontmatter else body
    path = skill_dir / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_bound_skill_is_injected_once_into_active_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        workspace,
        "demo",
        "Use the demo workflow.",
        frontmatter='name: demo\ndescription: "Demo skill"',
    )
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(skill_names=["demo", "demo"])

    assert prompt.count("### Skill: demo") == 1
    assert prompt.count("Use the demo workflow.") == 1


def test_unavailable_bound_skill_is_not_activated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        workspace,
        "missing",
        "Do not inject this.",
        frontmatter=(
            "name: missing\n"
            "description: Missing dependency\n"
            "metadata: '{\"yuanclaw\":{\"requires\":{\"bins\":[\"definitely-missing-bin\"]}}}'"
        ),
    )

    prompt = ContextBuilder(workspace).build_system_prompt(skill_names=["missing"])

    assert "### Skill: missing" not in prompt
    assert "Do not inject this." not in prompt
    assert '<skill available="false">' in prompt


def test_skill_frontmatter_uses_yaml_multiline_and_nested_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        workspace,
        "yaml-skill",
        "Instructions.",
        frontmatter=(
            "name: yaml-skill\n"
            "description: |\n"
            "  First line\n"
            "  Second line\n"
            "metadata:\n"
            "  yuanclaw:\n"
            "    always: true"
        ),
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "none")

    metadata = loader.get_skill_metadata("yaml-skill")

    assert metadata is not None
    assert metadata["description"] == "First line\nSecond line\n"
    assert loader.get_always_skills() == ["yaml-skill"]


def test_skill_frontmatter_preserves_false_with_crlf(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_file = _write_skill(workspace, "not-always", "Instructions.")
    skill_file.write_text(
        "---\r\nname: not-always\r\nalways: false\r\n---\r\nInstructions.",
        encoding="utf-8",
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "none")

    assert loader.get_skill_metadata("not-always") == {
        "name": "not-always",
        "always": False,
    }
    assert loader.get_always_skills() == []


def test_skill_platform_requirement_controls_availability(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    unavailable_os = "linux" if sys.platform.startswith("win") else "windows"
    _write_skill(
        workspace,
        "platform-specific",
        "Instructions.",
        frontmatter=(
            "name: platform-specific\n"
            f'metadata: \'{{"yuanclaw":{{"os":["{unavailable_os}"]}}}}\''
        ),
    )
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "none")

    assert loader.list_skills(filter_unavailable=True) == []
    summary = loader.build_skills_summary()
    assert '<skill available="false">' in summary
    assert f"OS: {unavailable_os}" in summary


def test_skill_context_enforces_per_skill_and_total_budgets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(workspace, "first", "A" * 100)
    _write_skill(workspace, "second", "B" * 100)
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "none")

    context = loader.load_skills_for_context(
        ["first", "second"],
        max_skill_chars=40,
        max_total_chars=110,
    )

    assert len(context) <= 110
    assert "skill truncated" in context
    assert context.count("A") <= 40
    assert context.count("B") <= 40


def test_skill_loader_rejects_invalid_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "none")

    assert loader.load_skill("../outside") is None
    assert loader.load_skill("nested/name") is None


def test_context_limits_image_count_and_file_size(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_header = b"\x89PNG\r\n\x1a\n"
    paths = []
    for index in range(4):
        path = workspace / f"{index}.png"
        path.write_bytes(image_header + bytes([index]) * 4)
        paths.append(str(path))

    builder = ContextBuilder(workspace)
    builder._MAX_IMAGES = 2
    builder._MAX_IMAGE_BYTES = 12
    builder._MAX_TOTAL_IMAGE_BYTES = 24
    content = builder._build_user_content("inspect", paths)

    assert isinstance(content, list)
    images = [item for item in content if item.get("type") == "image_url"]
    assert len(images) == 2
    decoded = [
        base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
        for item in images
    ]
    assert all(data.startswith(image_header) for data in decoded)


def test_context_skips_oversized_image_before_reading(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "large.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
    builder = ContextBuilder(workspace)
    builder._MAX_IMAGE_BYTES = 8
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(candidate: Path) -> bytes:
        if candidate == path:
            raise AssertionError("oversized image should not be read")
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    assert builder._build_user_content("inspect", [str(path)]) == "inspect"
