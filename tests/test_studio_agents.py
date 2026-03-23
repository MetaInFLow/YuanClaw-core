from yuanclaw.studio_agents import get_fixed_skills_for_session


def test_shifu_session_uses_fixed_skill() -> None:
    assert get_fixed_skills_for_session("studio:cowboy-shifu:thread-123") == [
        "neil-skills-creator"
    ]


def test_non_studio_session_has_no_fixed_skills() -> None:
    assert get_fixed_skills_for_session("cli:direct") == []
    assert get_fixed_skills_for_session("studio:cowboy-manong:thread-1") == []
