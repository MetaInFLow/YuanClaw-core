"""Built-in slash command handlers for YuanClaw."""

from __future__ import annotations

import asyncio
import os
import sys

from loguru import logger

from yuanclaw import __logo__, __version__
from yuanclaw.bus.events import OutboundMessage
from yuanclaw.command.router import CommandContext, CommandRouter
from yuanclaw.session.manager import Session
from yuanclaw.utils.helpers import build_status_content


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg

    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for task in tasks if not task.done() and task.cancel())
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the current YuanClaw process in-place."""
    msg = ctx.msg

    async def _do_restart() -> None:
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "yuanclaw"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="Restarting YuanClaw...")


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Return a human-readable runtime status snapshot."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__,
            model=loop.model,
            start_time=loop._start_time,
            last_usage=loop._last_usage,
            session_msg_count=len(session.messages),
            active_tasks=len(loop._active_tasks.get(session.key, [])),
            subagents_running=loop.subagents.get_running_count(),
            memory_window=loop.memory_window,
        ),
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session and archive the current one."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    lock = loop._consolidation_locks.setdefault(session.key, asyncio.Lock())
    loop._consolidating.add(session.key)
    try:
        async with lock:
            snapshot = session.messages[session.last_consolidated:]
            if snapshot:
                temp = Session(key=session.key)
                temp.messages = list(snapshot)
                temp.last_consolidated = session.last_consolidated
                if not await loop._consolidate_memory(temp, archive_all=True):
                    return OutboundMessage(
                        channel=ctx.msg.channel,
                        chat_id=ctx.msg.chat_id,
                        content="Memory archival failed, session not cleared. Please try again.",
                    )
    except Exception:
        logger.exception("/new archival failed for {}", session.key)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Memory archival failed, session not cleared. Please try again.",
        )
    finally:
        loop._consolidating.discard(session.key)

    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content="New session started.")


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available YuanClaw slash commands."""
    content = "\n".join([
        f"{__logo__} yuanclaw commands:",
        "/new - Start a new conversation",
        "/stop - Stop the current task",
        "/restart - Restart YuanClaw",
        "/status - Show runtime status",
        "/help - Show available commands",
    ])
    return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/help", cmd_help)
