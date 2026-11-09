"""Drawing: text, rectangles, and lines."""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import float_pixels
from griptape_nodes.image.operations.compositing import painted
from griptape_nodes.image.pipeline import DrawLine, DrawRectangle, DrawText, TextAlign

# The font OpenImageIO bundles and ``render_text`` uses when given no font name
_DEFAULT_FONT = ""

# Slack for float error in point-in-shape tests
_EPSILON = 1e-9


class TextSize(NamedTuple):
    """The box ``draw_text`` fills with a text, in pixels."""

    width: int
    height: int


def text_size(text: str, size: int, spacing: int = 4) -> TextSize:
    """Return the box ``draw_text`` fills with ``text`` at ``size``: the widest line, and the lines' height."""
    lines = text.split("\n")
    widths = [_line_width(line, size) for line in lines]
    metrics = _font_metrics(size)
    height = len(lines) * metrics.line_height + (len(lines) - 1) * spacing
    return TextSize(max(widths), height)


class _FontMetrics(NamedTuple):
    ascent: int
    line_height: int


def _font_metrics(size: int) -> _FontMetrics:
    # Tall and deep glyphs, so every line gets the same height whatever it holds
    roi = oiio.ImageBufAlgo.text_size("Ághjpqy|", size, _DEFAULT_FONT)
    return _FontMetrics(-roi.ybegin, roi.yend - roi.ybegin)


def _line_width(line: str, size: int) -> int:
    if not line.strip():
        return 0
    roi = oiio.ImageBufAlgo.text_size(line, size, _DEFAULT_FONT)
    return max(roi.xend, 0)


def with_text(buf: oiio.ImageBuf, step: DrawText) -> oiio.ImageBuf:
    """Draw text in the bundled font, antialiased.

    Raises:
        ValueError: If the size isn't positive or OpenImageIO can't render the text
    """
    if step.size <= 0:
        msg = f"Attempted to draw text at size {step.size}. Failed because the size must be at least 1 pixel."
        raise ValueError(msg)
    block = text_size(step.text, step.size, step.spacing)
    if block.width <= 0 or block.height <= 0:
        return buf.copy()
    metrics = _font_metrics(step.size)
    coverage = oiio.ImageBuf(oiio.ImageSpec(block.width, block.height, 1, oiio.FLOAT))
    for index, line in enumerate(step.text.split("\n")):
        if not line.strip():
            continue
        offset = 0
        width = _line_width(line, step.size)
        if step.align == TextAlign.CENTER:
            offset = (block.width - width) // 2
        elif step.align == TextAlign.RIGHT:
            offset = block.width - width
        baseline = metrics.ascent + index * (metrics.line_height + step.spacing)
        if not oiio.ImageBufAlgo.render_text(coverage, offset, baseline, line, step.size, _DEFAULT_FONT, (1.0,)):
            msg = f"Attempted to draw the text {line!r}. Failed because OpenImageIO reported: {oiio.geterror()}"
            raise ValueError(msg)
    return painted(buf, float_pixels(coverage)[:, :, 0], step.color, step.x, step.y)


def with_rectangle(buf: oiio.ImageBuf, step: DrawRectangle) -> oiio.ImageBuf:
    """Draw a box's fill, then its outline. An empty box draws nothing."""
    width = step.right - step.left
    height = step.bottom - step.top
    if width <= 0 or height <= 0:
        return buf.copy()
    result = buf
    if step.fill is not None:
        result = painted(result, np.ones((height, width), dtype=np.float32), step.fill, step.left, step.top)
    if step.outline is not None and step.width > 0:
        ring = np.ones((height, width), dtype=np.float32)
        inset = step.width
        if height > 2 * inset and width > 2 * inset:
            ring[inset:-inset, inset:-inset] = 0.0
        result = painted(result, ring, step.outline, step.left, step.top)
    if result is buf:
        return buf.copy()
    return result


def with_line(buf: oiio.ImageBuf, step: DrawLine) -> oiio.ImageBuf:
    """Draw a hard-edged line: pixels whose centers lie within the band ``width`` wide along the segment."""
    if step.width <= 0:
        return buf.copy()
    half = step.width / 2
    reach = math.ceil(half) + 1
    left = math.floor(min(step.x0, step.x1)) - reach
    top = math.floor(min(step.y0, step.y1)) - reach
    right = math.ceil(max(step.x0, step.x1)) + reach + 1
    bottom = math.ceil(max(step.y0, step.y1)) + reach + 1
    ys, xs = np.mgrid[top:bottom, left:right].astype(np.float64)
    dx = step.x1 - step.x0
    dy = step.y1 - step.y0
    length = math.hypot(dx, dy)
    if length == 0:
        # A point: the square ``width`` wide around it
        inside = (np.abs(xs - step.x0) * 2 < step.width + _EPSILON) & (np.abs(ys - step.y0) * 2 < step.width + _EPSILON)
    else:
        unit_x, unit_y = dx / length, dy / length
        along = (xs - step.x0) * unit_x + (ys - step.y0) * unit_y
        # Signed distance across the line, half-open so a width of n covers n rows of an axis-aligned line
        across = (xs - step.x0) * -unit_y + (ys - step.y0) * unit_x
        inside = (
            (along >= -_EPSILON)
            & (along <= length + _EPSILON)
            & (across >= -half - _EPSILON)
            & (across < half - _EPSILON)
        )
    return painted(buf, inside.astype(np.float32), step.color, left, top)
