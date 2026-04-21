from pathlib import Path

from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def test_studio_bound_skills_hide_global_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    skill_dir = workspace / "skills" / "base-builder"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("name: base-builder\n\nBuild things.", encoding="utf-8")

    builder = ContextBuilder(workspace)
    messages = builder.build_messages(
        history=[],
        current_message="你有哪些skill",
        skill_names=["base-builder"],
        channel="studio",
        chat_id="cowboy-draft:thread-1",
    )

    system_prompt = messages[0]["content"]
    assert "# Active Skills" in system_prompt
    assert "base-builder" in system_prompt
    assert "# Skills" not in system_prompt


def test_non_studio_prompt_still_includes_global_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    skill_dir = workspace / "skills" / "base-builder"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("name: base-builder\n\nBuild things.", encoding="utf-8")

    builder = ContextBuilder(workspace)
    messages = builder.build_messages(
        history=[],
        current_message="你有哪些skill",
        skill_names=["base-builder"],
        channel="cli",
        chat_id="direct",
    )

    system_prompt = messages[0]["content"]
    assert "# Skills" in system_prompt


def test_global_component_skills_are_visible(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    fake_home = tmp_path / "home"
    component_skill_dir = fake_home / ".cowdy" / "components" / "skills" / "base-builder"
    component_skill_dir.mkdir(parents=True, exist_ok=True)
    (component_skill_dir / "SKILL.md").write_text(
        "name: base-builder\n\ndescription: Build bases.",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    loader = SkillsLoader(workspace)
    skills = loader.list_skills(filter_unavailable=False)

    assert any(skill["name"] == "base-builder" and skill["source"] == "component" for skill in skills)
    assert "Build bases." in (loader.load_skill("base-builder") or "")


def test_cowdy_studio_cli_is_builtin_and_loadable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    loader = SkillsLoader(workspace)
    skills = loader.list_skills(filter_unavailable=False)

    assert (BUILTIN_SKILLS_DIR / "cowdy-studio-cli" / "SKILL.md").exists()
    assert any(
        skill["name"] == "cowdy-studio-cli" and skill["source"] in {"builtin", "component"}
        for skill in skills
    )
    assert "cowdy ext describe" in (loader.load_skill("cowdy-studio-cli") or "")
