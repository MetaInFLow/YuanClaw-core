from yuanclaw.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def test_cowdy_studio_cli_is_builtin_and_loadable(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    loader = SkillsLoader(workspace)
    skills = loader.list_skills(filter_unavailable=False)

    assert (BUILTIN_SKILLS_DIR / "cowdy-studio-cli" / "SKILL.md").exists()
    assert any(skill["name"] == "cowdy-studio-cli" and skill["source"] == "builtin" for skill in skills)
    assert "cowdy ext describe" in (loader.load_skill("cowdy-studio-cli") or "")
