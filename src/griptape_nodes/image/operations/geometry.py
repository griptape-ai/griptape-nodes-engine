"""Geometry: thumbnails, orientation, resizing, cropping, flipping, padding, and rotation."""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import (
    GRAYSCALE_ALPHA_CHANNELS,
    GRAYSCALE_CHANNELS,
    RGB_CHANNELS,
    RGBA_CHANNELS,
    alpha_channel,
    buffer_from_float,
    checked,
    clipped,
    color_channel_count,
    float_pixels,
)
from griptape_nodes.image.operations.channels import (
    color_float,
    from_rgba_float,
    is_color,
    layout_with,
    rgba_float,
    to_rgba,
)
from griptape_nodes.image.pipeline import Crop, Pad, ResizeFilter, Rotate, Thumbnail

# Pillow's filters as OpenImageIO names them. Pillow's bicubic is Catmull-Rom (a = -0.5); Hamming
# is a windowed sinc, of which Blackman-Harris is the nearest OpenImageIO has.
_FILTER_NAMES = {
    ResizeFilter.BOX: "box",
    ResizeFilter.BILINEAR: "triangle",
    ResizeFilter.HAMMING: "blackman-harris",
    ResizeFilter.BICUBIC: "catmull-rom",
    ResizeFilter.LANCZOS: "lanczos3",
}

# Alpha below which a filtered pixel counts as empty: under half an 8-bit step
_VISIBLE_ALPHA = 0.5 / 255

# Shrink factor from which a thumbnail uses a box filter, about 4x faster than the tent filter. On
# photographic content they differ by about 1/255; detail near the pixel scale (fine stripes,
# halftone) can alias by tens of levels, which previews accept.
_BOX_FILTER_MIN_SHRINK = 3

_FULL_TURN = 360
_QUARTER_TURN = 90


class _Size(NamedTuple):
    width: int
    height: int


class _Point(NamedTuple):
    x: float
    y: float


def thumbnail(buf: oiio.ImageBuf, step: Thumbnail) -> oiio.ImageBuf:
    """Shrink ``buf`` to fit ``step``'s bounds, keeping the aspect ratio. Never enlarges."""
    spec = buf.spec()
    scale = min(step.max_width / spec.width, step.max_height / spec.height)
    if scale >= 1:
        return buf.copy()
    width = max(1, round(spec.width * scale))
    height = max(1, round(spec.height * scale))
    roi = oiio.ROI(0, width, 0, height, 0, 1, 0, buf.nchannels)
    has_alpha = alpha_channel(buf) is not None
    # Filtering unassociated color would bleed the color of transparent pixels into the edges
    source = buf
    if has_alpha:
        source = oiio.ImageBufAlgo.premult(buf)
    # resize widens the filter by the shrink factor, so a tent filter antialiases. Lanczos3
    # takes about 8x longer on a 4000x3000 source.
    filter_name = "triangle"
    if scale * _BOX_FILTER_MIN_SHRINK <= 1:
        filter_name = "box"
    resized = checked(oiio.ImageBufAlgo.resize(source, filter_name, roi=roi))
    if has_alpha:
        resized = checked(oiio.ImageBufAlgo.unpremult(resized))
    return resized


def oriented(buf: oiio.ImageBuf, orientation: int) -> oiio.ImageBuf:  # noqa: PLR0911
    """Rotate and flip ``buf`` for an EXIF orientation value (2-8).

    Explicit transforms rather than ``ImageBufAlgo.reorient``, which reads the orientation from
    the buffer's own metadata.
    """
    if orientation == 2:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.flop(buf))
    if orientation == 3:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate180(buf))
    if orientation == 4:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.flip(buf))
    if orientation == 5:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.transpose(buf))
    if orientation == 6:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate90(buf))
    if orientation == 7:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate180(checked(oiio.ImageBufAlgo.transpose(buf))))
    if orientation == 8:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate270(buf))
    return buf.copy()


def resized(buf: oiio.ImageBuf, width: int, height: int, resize_filter: ResizeFilter) -> oiio.ImageBuf:
    """Scale to ``width`` x ``height``.

    Raises:
        ValueError: If the size isn't positive or OpenImageIO fails
    """
    if width <= 0 or height <= 0:
        msg = f"Attempted to resize an image to {width}x{height}. Failed because both sides must be at least 1 pixel."
        raise ValueError(msg)
    roi = oiio.ROI(0, width, 0, height, 0, 1, 0, buf.nchannels)
    has_alpha = alpha_channel(buf) is not None
    if resize_filter != ResizeFilter.NEAREST and has_alpha:
        # Premultiplied in float, so 8-bit color under low alpha survives the round trip
        source = checked(oiio.ImageBufAlgo.premult(buf.copy(oiio.FLOAT)))
        result = checked(oiio.ImageBufAlgo.resize(source, _FILTER_NAMES[resize_filter], roi=roi))
        pixels = _unpremultiplied(float_pixels(result), _brightest(buf))
        return buffer_from_float(clipped(pixels, buf), buf.spec().format)
    if resize_filter != ResizeFilter.NEAREST:
        return checked(oiio.ImageBufAlgo.resize(buf, _FILTER_NAMES[resize_filter], roi=roi))
    return checked(oiio.ImageBufAlgo.resample(buf, interpolate=False, roi=roi))


def cropped(buf: oiio.ImageBuf, step: Crop) -> oiio.ImageBuf:
    """Cut out the box ``step`` names, zero outside the image.

    Raises:
        ValueError: If the box is empty
    """
    if step.right <= step.left or step.bottom <= step.top:
        msg = (
            f"Attempted to crop an image to ({step.left}, {step.top}, {step.right}, {step.bottom}). "
            "Failed because the right and bottom edges must be past the left and top."
        )
        raise ValueError(msg)
    roi = oiio.ROI(step.left, step.right, step.top, step.bottom, 0, 1, 0, buf.nchannels)
    return checked(oiio.ImageBufAlgo.cut(buf, roi))


def flipped_horizontal(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Mirror left to right."""
    return checked(oiio.ImageBufAlgo.flop(buf))


def flipped_vertical(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Mirror top to bottom."""
    return checked(oiio.ImageBufAlgo.flip(buf))


def padded(buf: oiio.ImageBuf, step: Pad) -> oiio.ImageBuf:
    """Grow the canvas, filling the new border with ``step.fill``.

    Raises:
        ValueError: If a side is negative
    """
    if min(step.left, step.top, step.right, step.bottom) < 0:
        msg = (
            f"Attempted to pad an image by ({step.left}, {step.top}, {step.right}, {step.bottom}). "
            "Failed because padding can't be negative; crop instead."
        )
        raise ValueError(msg)
    spec = buf.spec()
    fill = color_float(step.fill)
    pixels = rgba_float(buf)
    canvas = np.empty(
        (spec.height + step.top + step.bottom, spec.width + step.left + step.right, RGBA_CHANNELS), dtype=np.float32
    )
    canvas[:, :] = fill
    canvas[step.top : step.top + spec.height, step.left : step.left + spec.width] = pixels
    channels = layout_with(buf.nchannels, color=is_color(fill), alpha=fill[3] < 1.0)
    return from_rgba_float(canvas, channels, spec.format)


def rotated(buf: oiio.ImageBuf, step: Rotate) -> oiio.ImageBuf:
    """Rotate counterclockwise as ``step`` says.

    Raises:
        ValueError: If the angle isn't a finite number
    """
    if not math.isfinite(step.degrees):
        msg = f"Attempted to rotate an image by {step.degrees} degrees. Failed because the angle must be a number."
        raise ValueError(msg)
    spec = buf.spec()
    degrees = step.degrees % _FULL_TURN
    quarter_turns = _quarter_turns(degrees)
    # Quarter turns move pixels without resampling, when the canvas can take the turned shape
    if quarter_turns is None or step.center is not None or not (step.expand or spec.width == spec.height):
        return _warped(buf, step, degrees)
    return _turned(buf, quarter_turns)


def _warped(buf: oiio.ImageBuf, step: Rotate, degrees: float) -> oiio.ImageBuf:
    """Rotate by resampling, filling where the turned image doesn't reach, as Pillow does."""
    spec = buf.spec()
    radians = math.radians(degrees)
    center = _Point(spec.width / 2, spec.height / 2)
    if step.center is not None:
        center = _Point(*step.center)
    size = _Size(spec.width, spec.height)
    target = center
    if step.expand:
        size = _expanded_size(spec.width, spec.height, radians, center)
        # Pillow moves the turned image by half the growth, which centers it on the default center
        target = _Point(center.x + (size.width - spec.width) / 2, center.y + (size.height - spec.height) / 2)

    matrix = _rotation_matrix(radians, center, target)
    roi = oiio.ROI(0, size.width, 0, size.height, 0, 1, 0, RGBA_CHANNELS)
    source = checked(oiio.ImageBufAlgo.premult(to_rgba(buf).copy(oiio.FLOAT)))
    premultiplied = float_pixels(_warp(source, matrix, step.resize_filter, roi))
    # How much of each pixel the turned image covers, so the fill shows only past its edges
    reach = oiio.ImageBuf(np.ones((spec.height, spec.width, 1), dtype=np.float32))
    reach_roi = oiio.ROI(0, size.width, 0, size.height, 0, 1, 0, 1)
    coverage = np.clip(float_pixels(_warp(reach, matrix, step.resize_filter, reach_roi)), 0.0, 1.0)

    has_alpha = alpha_channel(buf) is not None
    fill = step.fill
    if fill is None and not has_alpha:
        fill = (0, 0, 0)
    adds_alpha = has_alpha
    adds_color = False
    if fill is not None:
        fill_color = color_float(fill)
        fill_premultiplied = np.append(fill_color[:3] * fill_color[3], fill_color[3])
        premultiplied = premultiplied + fill_premultiplied * (1.0 - coverage)
        adds_alpha = has_alpha or fill_color[3] < 1.0
        adds_color = is_color(fill_color)
    pixels = _unpremultiplied(premultiplied, _brightest(buf))
    channels = layout_with(_without_alpha(buf.nchannels), color=adds_color, alpha=adds_alpha)
    return from_rgba_float(clipped(pixels, buf), channels, spec.format)


def _brightest(buf: oiio.ImageBuf) -> float:
    """The brightest color value in ``buf``, at least 1, so HDR highlights survive unpremultiplying."""
    color = float_pixels(buf)[:, :, : color_channel_count(buf)]
    return max(1.0, float(color.max()))


def _warp(
    source: oiio.ImageBuf, matrix: tuple[float, ...], resize_filter: ResizeFilter, roi: oiio.ROI
) -> oiio.ImageBuf:
    if resize_filter == ResizeFilter.NEAREST:
        return checked(oiio.ImageBufAlgo.warp(source, matrix, "box", filterwidth=1.0, roi=roi))
    return checked(oiio.ImageBufAlgo.warp(source, matrix, _FILTER_NAMES[resize_filter], roi=roi))


def _unpremultiplied(pixels: np.ndarray, brightest: float = 1.0) -> np.ndarray:
    """Divide premultiplied float pixels, alpha last, by their alpha, undoing filter ringing.

    Ringing can push alpha out of 0-1 and color past what the alpha allows, which division by a
    tiny alpha blows up. So alpha is clamped to 0-1 and premultiplied color to 0 through alpha
    times ``brightest``, the source's brightest color value.
    """
    alpha = np.clip(pixels[:, :, -1:], 0.0, 1.0)
    color = np.clip(pixels[:, :, :-1], 0.0, alpha * brightest)
    visible = alpha > _VISIBLE_ALPHA
    safe_alpha = np.where(visible, alpha, 1.0)
    color = np.where(visible, color / safe_alpha, 0.0)
    return np.concatenate([color, alpha], axis=2).astype(np.float32)


def _without_alpha(channels: int) -> int:
    if channels <= GRAYSCALE_ALPHA_CHANNELS:
        return GRAYSCALE_CHANNELS
    return RGB_CHANNELS


def _quarter_turns(degrees: float) -> int | None:
    if degrees % _QUARTER_TURN != 0:
        return None
    return int(degrees // _QUARTER_TURN)


def _turned(buf: oiio.ImageBuf, quarter_turns: int) -> oiio.ImageBuf:
    """Rotate counterclockwise by whole quarter turns, moving pixels without resampling."""
    if quarter_turns == 1:
        # OpenImageIO turns clockwise
        return checked(oiio.ImageBufAlgo.rotate270(buf))
    if quarter_turns == 2:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate180(buf))
    if quarter_turns == 3:  # noqa: PLR2004
        return checked(oiio.ImageBufAlgo.rotate90(buf))
    return buf.copy()


def _expanded_size(width: int, height: int, radians: float, center: _Point) -> _Size:
    """The canvas that holds a ``width`` x ``height`` image turned about ``center``, as Pillow sizes it.

    Pillow turns the corners and spans them from the floor of the least to the ceiling of the greatest.
    """
    cos = math.cos(radians)
    sin = math.sin(radians)
    xs = []
    ys = []
    for x, y in ((0, 0), (width, 0), (width, height), (0, height)):
        dx = x - center.x
        dy = y - center.y
        # Rounded first, so float error doesn't push a whole size up a pixel
        xs.append(round(center.x + dx * cos + dy * sin, 6))
        ys.append(round(center.y - dx * sin + dy * cos, 6))
    new_width = math.ceil(max(xs)) - math.floor(min(xs))
    new_height = math.ceil(max(ys)) - math.floor(min(ys))
    return _Size(max(new_width, 1), max(new_height, 1))


def _rotation_matrix(radians: float, center: _Point, target: _Point) -> tuple[float, ...]:
    """A 3x3 row-vector matrix turning counterclockwise on screen about ``center`` and moving it to ``target``.

    With y pointing down, a counterclockwise turn maps (x, y) to (x cos + y sin, -x sin + y cos).
    """
    cos = math.cos(radians)
    sin = math.sin(radians)
    offset_x = target.x - (center.x * cos + center.y * sin)
    offset_y = target.y - (-center.x * sin + center.y * cos)
    return (cos, -sin, 0.0, sin, cos, 0.0, offset_x, offset_y, 1.0)
