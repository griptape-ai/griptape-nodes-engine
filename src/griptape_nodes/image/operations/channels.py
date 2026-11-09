"""Channel layout conversions: RGB, RGBA, gray, 8-bit, and new solid images."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import (
    GRAYSCALE_ALPHA_CHANNELS,
    GRAYSCALE_CHANNELS,
    RGB_CHANNELS,
    RGBA_CHANNELS,
    buffer_from_float,
    buffer_from_pixels,
    checked,
    float_pixels,
    is_float,
)
from griptape_nodes.image.operations.color import LUMA, display_referred

if TYPE_CHECKING:
    from griptape_nodes.image.pipeline import Color

_MAX_8_BIT = 255.0


def to_rgb(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return three-channel color, gray copied to each channel, alpha discarded."""
    order: tuple[int | float, ...] = (0, 1, 2)
    if buf.nchannels in (GRAYSCALE_CHANNELS, GRAYSCALE_ALPHA_CHANNELS):
        order = (0, 0, 0)
    return checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B")))


def to_rgba(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return four-channel color, with an opaque alpha if there was none."""
    channels = buf.nchannels
    if channels == RGBA_CHANNELS:
        return buf.copy()
    if channels == GRAYSCALE_CHANNELS:
        order: tuple[int | float, ...] = (0, 0, 0, 1.0)
    elif channels == GRAYSCALE_ALPHA_CHANNELS:
        order = (0, 0, 0, 1)
    else:
        order = (0, 1, 2, 1.0)
    return checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B", "A")))


def to_gray(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return one-channel luma, discarding alpha."""
    pixels = float_pixels(buf)
    if buf.nchannels <= GRAYSCALE_ALPHA_CHANNELS:
        gray = pixels[:, :, :1]
    else:
        gray = (pixels[:, :, :RGB_CHANNELS] @ LUMA)[:, :, np.newaxis]
    return buffer_from_float(gray, buf.spec().format)


def to_uint8(buf: oiio.ImageBuf, *, holds_display_values: bool) -> oiio.ImageBuf:
    """Return 8 bits per channel. Scene-linear float pixels get the sRGB curve first, as they do when encoded for display.

    Args:
        buf: The pixels
        holds_display_values: Whether float pixels already hold display values, so they're only rescaled
    """
    if str(buf.spec().format) == "uint8":
        return buf.copy()
    if is_float(buf) and not holds_display_values:
        buf = display_referred(buf)
    return checked(buf.copy(oiio.UINT8))


def rgba_float(buf: oiio.ImageBuf) -> np.ndarray:
    """Return the pixels as height x width x 4 float RGBA, opaque if they had no alpha."""
    return float_pixels(to_rgba(buf))


def from_rgba_float(pixels: np.ndarray, channels: int, pixel_format: oiio.TypeDesc) -> oiio.ImageBuf:
    """Store height x width x 4 float RGBA as ``channels`` channels of ``pixel_format``.

    Gray layouts take the red channel, which is exact for RGBA that ``to_rgba`` made from gray.
    """
    if channels == GRAYSCALE_CHANNELS:
        layout = pixels[:, :, :1]
    elif channels == GRAYSCALE_ALPHA_CHANNELS:
        layout = pixels[:, :, [0, 3]]
    elif channels == RGB_CHANNELS:
        layout = pixels[:, :, :RGB_CHANNELS]
    else:
        layout = pixels
    return buffer_from_float(layout, pixel_format)


def color_float(color: Color) -> np.ndarray:
    """Return an 8-bit gray, gray-alpha, RGB, or RGBA color as float RGBA 0-1.

    Raises:
        ValueError: If the color doesn't have 1-4 components
    """
    if len(color) == GRAYSCALE_CHANNELS:
        rgba = (color[0], color[0], color[0], 255)
    elif len(color) == GRAYSCALE_ALPHA_CHANNELS:
        rgba = (color[0], color[0], color[0], color[1])
    elif len(color) == RGB_CHANNELS:
        rgba = (*color, 255)
    elif len(color) == RGBA_CHANNELS:
        rgba = tuple(color)
    else:
        msg = f"Attempted to use the color {color}. Failed because a color needs 1-4 values from 0 to 255."
        raise ValueError(msg)
    if not all(0 <= value <= _MAX_8_BIT for value in rgba):
        msg = f"Attempted to use the color {color}. Failed because a color needs 1-4 values from 0 to 255."
        raise ValueError(msg)
    return np.array(rgba, dtype=np.float32) / _MAX_8_BIT


def is_color(color: np.ndarray) -> bool:
    """Whether an RGBA color is not a shade of gray."""
    return not (color[0] == color[1] == color[2])


def layout_with(channels: int, *, color: bool, alpha: bool) -> int:
    """The channel count holding ``channels`` plus color and/or alpha, if needed."""
    has_color = channels >= RGB_CHANNELS or color
    has_alpha = channels in (GRAYSCALE_ALPHA_CHANNELS, RGBA_CHANNELS) or alpha
    if has_color and has_alpha:
        return RGBA_CHANNELS
    if has_color:
        return RGB_CHANNELS
    if has_alpha:
        return GRAYSCALE_ALPHA_CHANNELS
    return GRAYSCALE_CHANNELS


def new_image(width: int, height: int, color: Color) -> oiio.ImageBuf:
    """Return an 8-bit image of one color, with as many channels as ``color`` has values.

    Raises:
        ValueError: If the size isn't positive or the color doesn't have 1-4 values
    """
    if width <= 0 or height <= 0:
        msg = f"Attempted to make a {width}x{height} image. Failed because both sides must be at least 1 pixel."
        raise ValueError(msg)
    color_float(color)
    pixels = np.empty((height, width, len(color)), dtype=np.uint8)
    pixels[:, :] = color
    return buffer_from_pixels(pixels)
