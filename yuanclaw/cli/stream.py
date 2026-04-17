"""Streaming renderer for CLI output."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from yuanclaw import __logo__
from yuanclaw.utils.helpers import strip_think


def _make_console() -> Console:
    return Console(file=sys.stdout)


class ThinkingSpinner:
    """Spinner that shows "yuanclaw is thinking..." with pause support."""

    def __init__(self, console: Console | None = None):
        self._console = console or _make_console()
        self._spinner = self._console.status("[dim]yuanclaw is thinking...[/dim]", spinner="dots")
        self._active = False

    def __enter__(self):
        self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        self._spinner.stop()
        return False

    def pause(self):
        """Temporarily stop the spinner so another line can print cleanly."""

        @contextmanager
        def _ctx():
            if self._active:
                self._spinner.stop()
            try:
                yield
            finally:
                if self._active:
                    self._spinner.start()

        return _ctx()


class StreamRenderer:
    """Rich Live renderer for streaming assistant text."""

    def __init__(self, render_markdown: bool = True, show_spinner: bool = True):
        self._render_markdown = render_markdown
        self._show_spinner = show_spinner
        self._buffer = ""
        self._live: Live | None = None
        self._last_refresh = 0.0
        self._spinner: ThinkingSpinner | None = None
        self.streamed = False
        if self._show_spinner:
            self._spinner = ThinkingSpinner()
            self._spinner.__enter__()

    def _renderable(self):
        content = strip_think(self._buffer)
        if not content:
            return Text("")
        return Markdown(content) if self._render_markdown else Text(content)

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    async def on_delta(self, delta: str) -> None:
        """Append a streamed text delta and update the live view."""
        if not delta:
            return
        self.streamed = True
        self._buffer += delta
        content = strip_think(self._buffer)
        if not content:
            return

        if self._live is None:
            self._stop_spinner()
            console = _make_console()
            console.print()
            console.print(f"[cyan]{__logo__} yuanclaw[/cyan]")
            self._live = Live(self._renderable(), console=console, auto_refresh=False)
            self._live.start()

        now = time.monotonic()
        if "\n" in delta or (now - self._last_refresh) > 0.05:
            self._live.update(self._renderable())
            self._live.refresh()
            self._last_refresh = now

    async def on_end(self, *, resuming: bool = False) -> None:
        """Stop the live renderer when a streamed turn ends."""
        if self._live is not None:
            self._live.update(self._renderable())
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._stop_spinner()
        if resuming:
            self._buffer = ""
            if self._show_spinner:
                self._spinner = ThinkingSpinner()
                self._spinner.__enter__()
        else:
            _make_console().print()
