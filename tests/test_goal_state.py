"""Tests for sustained-goal session metadata helpers."""

from __future__ import annotations

from yuanclaw.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_state_runtime_lines,
    goal_state_ws_blob,
    parse_goal_state,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from yuanclaw.session.manager import SessionManager


def test_goal_state_runtime_lines_include_active_objective() -> None:
    metadata = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship the core-only nanobot feature sync.",
            "ui_summary": "core sync",
        }
    }

    lines = goal_state_runtime_lines(metadata)

    assert "Goal (active):" in lines
    assert "Ship the core-only nanobot feature sync." in lines
    assert "Summary: core sync" in lines


def test_goal_state_reads_legacy_thread_goal_key() -> None:
    metadata = {
        "thread_goal": {
            "status": "active",
            "objective": "Legacy objective.",
        }
    }

    assert sustained_goal_active(metadata) is True
    assert "Legacy objective." in goal_state_runtime_lines(metadata)


def test_goal_state_key_takes_precedence_and_legacy_can_be_discarded() -> None:
    metadata = {
        GOAL_STATE_KEY: {"status": "active", "objective": "New objective."},
        "thread_goal": {"status": "active", "objective": "Old objective."},
    }

    lines = goal_state_runtime_lines(metadata)
    discard_legacy_goal_state_key(metadata)

    assert "New objective." in lines
    assert "Old objective." not in "\n".join(lines)
    assert "thread_goal" not in metadata


def test_parse_goal_state_accepts_json_string() -> None:
    assert parse_goal_state('{"status":"active","objective":"x"}') == {
        "status": "active",
        "objective": "x",
    }


def test_goal_state_ws_blob_returns_bounded_active_snapshot() -> None:
    metadata = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "x" * 500,
            "ui_summary": "summary" * 40,
            "internal": "not exposed",
        }
    }

    blob = goal_state_ws_blob(metadata)

    assert blob["active"] is True
    assert blob["status"] == "active"
    assert blob["objective"] == ("x" * 240) + "..."
    assert blob["ui_summary"] == ("summary" * 20)[:120]
    assert "internal" not in blob


def test_goal_state_ws_blob_returns_inactive_snapshot() -> None:
    assert goal_state_ws_blob({}) == {"active": False}
    assert goal_state_ws_blob({GOAL_STATE_KEY: {"status": "completed"}}) == {"active": False}


def test_runner_wall_llm_timeout_disabled_when_goal_active(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("cli:direct")
    session.metadata[GOAL_STATE_KEY] = {"status": "active", "objective": "Keep going."}
    sessions.save(session)

    assert runner_wall_llm_timeout_s(sessions, "cli:direct") == 0.0
    assert runner_wall_llm_timeout_s(sessions, "cli:direct", metadata={}) is None
