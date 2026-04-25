"""Studio agent bindings for fixed runtime behavior."""

from __future__ import annotations

STUDIO_AGENT_FIXED_SKILLS: dict[str, list[str]] = {
    "cowboy-shifu": ["cowdy-studio-cli"],
}


def get_fixed_skills_for_session(session_key: str | None) -> list[str]:
    """Return fixed skill names for a studio session key, if any."""
    if not session_key:
        return []

    parts = str(session_key).split(":", 2)
    if len(parts) < 3 or parts[0] != "studio":
        return []

    return list(STUDIO_AGENT_FIXED_SKILLS.get(parts[1], []))
