from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuanclaw.bus.events import InboundMessage
from yuanclaw.command.builtin import cmd_restart
from yuanclaw.command.router import CommandContext


def _restart_context(loop) -> CommandContext:
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="direct",
        content="/restart",
    )
    return CommandContext(
        msg=msg,
        session=None,
        key=msg.session_key,
        raw="/restart",
        loop=loop,
    )


@pytest.mark.asyncio
async def test_restart_command_delegates_to_runtime_lifecycle() -> None:
    handler = AsyncMock(return_value=True)

    response = await cmd_restart(_restart_context(SimpleNamespace(restart_handler=handler)))

    handler.assert_awaited_once()
    assert response.content == "Restarting YuanClaw..."


@pytest.mark.asyncio
async def test_restart_command_reports_missing_host_support() -> None:
    response = await cmd_restart(_restart_context(SimpleNamespace()))

    assert "unavailable" in response.content
