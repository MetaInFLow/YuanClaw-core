"""Tests for sustained-goal tools."""

from __future__ import annotations

import pytest

from yuanclaw.agent.tools.long_task import CompleteGoalTool, LongTaskTool
from yuanclaw.session.goal_state import GOAL_STATE_KEY
from yuanclaw.session.manager import SessionManager


@pytest.mark.asyncio
async def test_long_task_records_goal_metadata_and_persists(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    tool = LongTaskTool(sessions)
    tool.set_context("cli", "direct")

    result = await tool.execute(
        goal="Implement the core-only feature sync until tests pass.",
        ui_summary="core sync",
    )

    reloaded = SessionManager(tmp_path).get_or_create("cli:direct")
    goal = reloaded.metadata[GOAL_STATE_KEY]
    assert "Goal recorded" in result
    assert goal["status"] == "active"
    assert goal["objective"] == "Implement the core-only feature sync until tests pass."
    assert goal["ui_summary"] == "core sync"
    assert "started_at" in goal


@pytest.mark.asyncio
async def test_long_task_refuses_to_replace_active_goal_without_completion(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    tool = LongTaskTool(sessions)
    tool.set_context("cli", "direct")

    await tool.execute(goal="First goal.")
    result = await tool.execute(goal="Second goal.")

    session = sessions.get_or_create("cli:direct")
    assert "already active" in result
    assert session.metadata[GOAL_STATE_KEY]["objective"] == "First goal."


@pytest.mark.asyncio
async def test_complete_goal_marks_active_goal_completed_and_persists(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    start = LongTaskTool(sessions)
    complete = CompleteGoalTool(sessions)
    start.set_context("cli", "direct")
    complete.set_context("cli", "direct")

    await start.execute(goal="Finish the slice.")
    result = await complete.execute(recap="Implemented and verified.")

    reloaded = SessionManager(tmp_path).get_or_create("cli:direct")
    goal = reloaded.metadata[GOAL_STATE_KEY]
    assert "Goal marked complete" in result
    assert goal["status"] == "completed"
    assert goal["objective"] == "Finish the slice."
    assert goal["recap"] == "Implemented and verified."
    assert "completed_at" in goal


@pytest.mark.asyncio
async def test_goal_tools_require_routing_context(tmp_path) -> None:
    sessions = SessionManager(tmp_path)

    assert "requires an active chat session" in await LongTaskTool(sessions).execute(goal="x")
    assert "requires an active chat session" in await CompleteGoalTool(sessions).execute()
