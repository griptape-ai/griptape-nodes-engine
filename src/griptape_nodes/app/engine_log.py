from __future__ import annotations

import colorsys
import functools
import hashlib
import logging
from datetime import UTC
from typing import TYPE_CHECKING, cast

from rich.logging import RichHandler
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.traceback import Traceback


@functools.cache
def _prefix_to_color(prefix: str) -> str:
    """Deterministically map a log prefix to a hex color.

    Uses the first byte of the MD5 digest as a hue value, with fixed lightness
    and saturation so every color is vivid and readable on dark terminals.
    Cached so the hash runs once per unique prefix.
    """
    digest = hashlib.md5(prefix.encode(), usedforsecurity=False).digest()
    hue = digest[0] / 255.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.72, 0.85)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


class _EngineRoleFilter(logging.Filter):
    """Injects engine_prefix into every log record to identify which engine produced the log."""

    def __init__(self) -> None:
        super().__init__()
        self.prefix: str = ""

    def filter(self, record: logging.LogRecord) -> bool:
        record.engine_prefix = self.prefix  # type: ignore[attr-defined]
        return True


class _EngineRoleHandler(RichHandler):
    """RichHandler that inserts a worker engine designator as its own column between log level and message.

    Construct this with ``markup=False``. Log messages carry user- and model-authored text
    (prompts, tool arguments, tool results, exception strings), and Rich reads a bracketed
    sequence such as ``[/SECTION]`` as a closing style tag. With no matching open tag that
    raises MarkupError from ``RichHandler.emit``, which guards only the console write and not
    the markup parse -- so the error escapes the ``logger.info(...)`` call and takes down the
    caller. Colour, the time column, the designator column and rich tracebacks are all
    unaffected by ``markup=False``; a line that genuinely wants markup opts in per-record
    with ``logger.info(..., extra={"markup": True})``.
    """

    _COLUMN_WIDTH = 15  # display width for "Worker-XXXXXXXX"

    def render(  # type: ignore[override]
        self,
        *,
        record: logging.LogRecord,
        traceback: object,
        message_renderable: object,
    ) -> object:
        prefix: str = getattr(record, "engine_prefix", "")
        if not prefix:
            from rich.console import ConsoleRenderable

            return super().render(  # type: ignore[call-arg]
                record=record,
                traceback=cast("Traceback | None", traceback),
                message_renderable=cast("ConsoleRenderable", message_renderable),
            )

        from datetime import datetime

        from rich.console import ConsoleRenderable, Group

        output = Table.grid(padding=(0, 1))
        output.expand = True
        output.add_column(style="log.time")
        output.add_column(style="log.level", width=self._log_render.level_width)  # type: ignore[attr-defined]
        output.add_column(width=self._COLUMN_WIDTH, no_wrap=True)
        output.add_column(ratio=1, style="log.message", overflow="fold")

        log_time = datetime.fromtimestamp(record.created, tz=UTC)
        # Mirror super().render(): prefer formatter.datefmt (set by logging.basicConfig) over the
        # handler-level default, so the time column matches orchestrator log formatting.
        time_format = (None if self.formatter is None else self.formatter.datefmt) or self._log_render.time_format  # type: ignore[attr-defined]
        formatted_time = time_format(log_time) if callable(time_format) else Text(log_time.strftime(time_format))
        level = self.get_level_text(record)
        designator = Text(f"{prefix:<{self._COLUMN_WIDTH}}", style=f"bold {_prefix_to_color(prefix)}")
        typed_message = cast("ConsoleRenderable", message_renderable)
        typed_traceback = cast("Traceback | None", traceback)
        msg_cell: ConsoleRenderable = (
            Group(typed_message, typed_traceback) if typed_traceback is not None else typed_message
        )

        output.add_row(formatted_time, level, designator, msg_cell)
        return output
