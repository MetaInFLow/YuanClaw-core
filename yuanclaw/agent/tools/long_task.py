"""Sustained-goal tools for long-running objectives."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_state_raw,
    parse_goal_state,
)
from yuanclaw.session.manager import SessionManager


def _iso_now() -> str:
    return datetime.now().isoformat()


class _GoalToolBase:
    """Shared routing context and session lookup."""

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions
        self._route: ContextVar[tuple[str, str]] = ContextVar(
            f"goal_route_{id(self)}",
            default=("", ""),
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set current chat routing context."""
        self._route.set((channel, chat_id))

    def _session(self):
        channel, chat_id = self._route.get()
        if not channel or not chat_id:
            return None
        return self._sessions.get_or_create(f"{channel}:{chat_id}")


class LongTaskTool(Tool, _GoalToolBase):
    """Register an active sustained objective on the current session."""

    def __init__(self, sessions: SessionManager) -> None:
        _GoalToolBase.__init__(self, sessions)

    @property
    def name(self) -> str:
        return "long_task"

    @property
    def description(self) -> str:
        return (
            "Mark this chat thread as a sustained long-running objective. "
            "Use an idempotent, self-contained, bounded goal. The active goal is "
            "mirrored in Runtime Context until complete_goal is called."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Sustained objective for this chat thread.",
                    "maxLength": 12000,
                },
                "ui_summary": {
                    "type": ["string", "null"],
                    "description": "Optional one-line label for session lists/logs.",
                    "maxLength": 120,
                },
            },
            "required": ["goal"],
        }

    async def execute(self, goal: str, ui_summary: str | None = None, **kwargs: Any) -> str:
        session = self._session()
        if session is None:
            return "Error: long_task requires an active chat session (missing routing context)."

        prior = parse_goal_state(goal_state_raw(session.metadata))
        if isinstance(prior, dict) and prior.get("status") == "active":
            return (
                "Error: a sustained goal is already active. "
                "Use complete_goal when finished, or ask the user before replacing it."
            )

        summary = (ui_summary or "").strip()[:120]
        session.metadata[GOAL_STATE_KEY] = {
            "status": "active",
            "objective": goal.strip(),
            "ui_summary": summary,
            "started_at": _iso_now(),
        }
        discard_legacy_goal_state_key(session.metadata)
        self._sessions.save(session)

        extra = f"\nSummary line: {summary}" if summary else ""
        return (
            "Goal recorded. Keep working toward the objective using ordinary tools. "
            "When fully done and verified, call complete_goal with a short recap."
            f"{extra}"
        )


class CompleteGoalTool(Tool, _GoalToolBase):
    """Mark the current sustained objective as completed."""

    def __init__(self, sessions: SessionManager) -> None:
        _GoalToolBase.__init__(self, sessions)

    @property
    def name(self) -> str:
        return "complete_goal"

    @property
    def description(self) -> str:
        return (
            "End bookkeeping for the active sustained goal. Use when the objective "
            "is fully achieved and verified, cancelled, redirected, or replaced."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recap": {
                    "type": ["string", "null"],
                    "description": "Brief factual recap of what happened.",
                    "maxLength": 8000,
                }
            },
            "required": [],
        }

    async def execute(self, recap: str | None = None, **kwargs: Any) -> str:
        session = self._session()
        if session is None:
            return "Error: complete_goal requires an active chat session."

        prior = parse_goal_state(goal_state_raw(session.metadata))
        if not isinstance(prior, dict) or prior.get("status") != "active":
            return "No active goal to complete."

        ended = _iso_now()
        session.metadata[GOAL_STATE_KEY] = {
            **prior,
            "status": "completed",
            "completed_at": ended,
            "recap": (recap or "").strip(),
        }
        discard_legacy_goal_state_key(session.metadata)
        self._sessions.save(session)

        tail = (recap or "").strip()
        if tail:
            return f"Goal marked complete ({ended}). Recap:\n{tail}"
        return f"Goal marked complete ({ended})."
