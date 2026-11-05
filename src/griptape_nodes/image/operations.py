"""Pixel operations behind ``GriptapeImagePipeline`` steps, on OpenImageIO buffers.

Buffers hold unassociated alpha, last: gray (Y), gray and alpha (Y, A), RGB, or RGBA. Operations
that filter or composite premultiply first, so the color of transparent pixels doesn't bleed into
their neighbors, and return a buffer of the source's pixel type.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.pipeline import (
    Brightness,
    Color,
    Composite,
    Contrast,
    Crop,
    DrawLine,
    DrawRectangle,
    DrawText,
    Pad,
    ResizeFilter,
    Rotate,
    Saturation,
    TextAlign,
)

GRAYSCALE_CHANNELS = 1
GRAYSCALE_ALPHA_CHANNELS = 2
RGB_CHANNELS = 3
RGBA_CHANNELS = 4

_UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"
_CHANNEL_NAMES = {
    GRAYSCALE_CHANNELS: ("Y",),
    GRAYSCALE_ALPHA_CHANNELS: ("Y", "A"),
    RGB_CHANNELS: ("R", "G", "B"),
    RGBA_CHANNELS: ("R", "G", "B", "A"),
}

# ITU-R 601 luma, the weights Pillow's "L" conversion uses
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# Pillow's filters as OpenImageIO names them. Pillow's bicubic is Catmull-Rom (a = -0.5); Hamming
# is a windowed sinc, of which Blackman-Harris is the nearest OpenImageIO has.
_FILTER_NAMES = {
    ResizeFilter.BOX: "box",
    ResizeFilter.BILINEAR: "triangle",
    ResizeFilter.HAMMING: "blackman-harris",
    ResizeFilter.BICUBIC: "catmull-rom",
    ResizeFilter.LANCZOS: "lanczos3",
}

# Pillow's ImageFilter.FIND_EDGES
_FIND_EDGES_KERNEL = np.array([[-1, -1, -1], [-1, 8, -1], [-1, -1, -1]], dtype=np.float32)

# Box blurs that approximate a Gaussian blur, as Pillow's ``GaussianBlur`` runs
_BOX_PASSES = 3

# The font OpenImageIO bundles and ``render_text`` uses when given no font name
_DEFAULT_FONT = ""

# Slack for float error in point-in-shape tests
_EPSILON = 1e-9

# Alpha below which a filtered pixel counts as empty: under half an 8-bit step
_VISIBLE_ALPHA = 0.5 / 255

_FULL_TURN = 360
_QUARTER_TURN = 90
_MAX_8_BIT = 255.0


class TextSize(NamedTuple):
    """The box ``draw_text`` fills with a text, in pixels."""

    width: int
    height: int


class _Placement(NamedTuple):
    """Where a layer lands on a canvas: the canvas slices it covers and the matching layer slices."""

    target: tuple[slice, slice]
    source: tuple[slice, slice]


class _Size(NamedTuple):
    width: int
    height: int


class _Point(NamedTuple):
    x: float
    y: float


# ---------------------------------------------------------------------------
# Buffers


def checked(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return ``buf``, raising ValueError if the OpenImageIO call that made it failed."""
    if buf.has_error:
        msg = f"Attempted to transform an image. Failed because OpenImageIO reported: {buf.geterror()}"
        raise ValueError(msg)
    return buf


def alpha_channel(buf: oiio.ImageBuf) -> int | None:
    """Return the index of ``buf``'s alpha channel, or None if it has none."""
    alpha = buf.spec().alpha_channel
    if alpha < 0:
        return None
    return alpha


def max_value(dtype: np.dtype) -> float:
    """Return the value of full intensity for pixels of ``dtype``: the integer maximum, or 1 for float."""
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def buffer_from_pixels(pixels: np.ndarray) -> oiio.ImageBuf:
    """Wrap a height x width (x channels) array of unassociated pixels, naming its channels by count.

    Raises:
        ValueError: If the array isn't 2-D or 3-D with 1-4 channels
    """
    if pixels.ndim == 2:  # noqa: PLR2004
        pixels = pixels[:, :, np.newaxis]
    # OpenImageIO reads only native byte order
    pixels = pixels.astype(pixels.dtype.newbyteorder("="), copy=False)
    if pixels.ndim != 3 or pixels.shape[2] not in _CHANNEL_NAMES or 0 in pixels.shape:  # noqa: PLR2004
        msg = (
            f"Attempted to make an image from pixels shaped {pixels.shape}. Failed because pixels must be "
            "height x width x channels, with 1-4 channels."
        )
        raise ValueError(msg)
    buf = oiio.ImageBuf(np.ascontiguousarray(pixels))
    _name_channels(buf)
    return buf


def float_pixels(buf: oiio.ImageBuf) -> np.ndarray:
    """Return the pixels as a height x width x channels float32 array, integer types scaled to 0-1.

    Raises:
        ValueError: If OpenImageIO cannot read the pixels
    """
    pixels = buf.get_pixels(oiio.FLOAT)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to read an image's pixels. Failed because OpenImageIO could not read them: {buf.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    return pixels.reshape(buf.spec().height, buf.spec().width, buf.nchannels)


def native_pixels(buf: oiio.ImageBuf) -> np.ndarray:
    """Return a height x width x channels copy of the pixels in their own type.

    Raises:
        ValueError: If OpenImageIO cannot read the pixels
    """
    spec = buf.spec()
    pixels = buf.get_pixels(spec.format)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to read an image's pixels. Failed because OpenImageIO could not read them: {buf.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    return pixels.reshape(spec.height, spec.width, buf.nchannels).copy()


def buffer_from_float(pixels: np.ndarray, pixel_format: oiio.TypeDesc) -> oiio.ImageBuf:
    """Store float 0-1 pixels in a buffer of ``pixel_format``, rounding and clamping for integer types."""
    height, width, channels = pixels.shape
    buf = oiio.ImageBuf(oiio.ImageSpec(width, height, channels, pixel_format))
    buf.set_pixels(oiio.ROI(0, width, 0, height, 0, 1, 0, channels), np.ascontiguousarray(pixels, dtype=np.float32))
    _name_channels(buf)
    return checked(buf)


def _name_channels(buf: oiio.ImageBuf) -> None:
    channels = buf.nchannels
    buf.specmod().channelnames = _CHANNEL_NAMES[channels]
    if channels in (GRAYSCALE_ALPHA_CHANNELS, RGBA_CHANNELS):
        buf.specmod().alpha_channel = channels - 1
    else:
        buf.specmod().alpha_channel = -1
    buf.specmod().attribute(_UNASSOCIATED_ALPHA, 1)


def is_integer(buf: oiio.ImageBuf) -> bool:
    """Whether ``buf`` stores integer pixels, which clamp to their range."""
    return str(buf.spec().format) not in ("half", "float", "double")


def _clipped(pixels: np.ndarray, buf: oiio.ImageBuf) -> np.ndarray:
    """Clip float pixels to 0-1 when they'll be stored in ``buf``'s integer type; float types keep the full range."""
    if is_integer(buf):
        return np.clip(pixels, 0.0, 1.0)
    return pixels


# ---------------------------------------------------------------------------
# Channel layouts


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
        gray = (pixels[:, :, :RGB_CHANNELS] @ _LUMA)[:, :, np.newaxis]
    return buffer_from_float(gray, buf.spec().format)


def _rgba_float(buf: oiio.ImageBuf) -> np.ndarray:
    """Return the pixels as height x width x 4 float RGBA, opaque if they had no alpha."""
    return float_pixels(to_rgba(buf))


def _from_rgba_float(pixels: np.ndarray, channels: int, pixel_format: oiio.TypeDesc) -> oiio.ImageBuf:
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


def _color_float(color: Color) -> np.ndarray:
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


def _is_color(color: np.ndarray) -> bool:
    return not (color[0] == color[1] == color[2])


def _layout_with(channels: int, *, color: bool, alpha: bool) -> int:
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
    _color_float(color)
    pixels = np.empty((height, width, len(color)), dtype=np.uint8)
    pixels[:, :] = color
    return buffer_from_pixels(pixels)


# ---------------------------------------------------------------------------
# Compositing


def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    """Porter-Duff ``top`` over ``bottom``, both float RGBA with unassociated alpha."""
    top_alpha = top[:, :, 3:]
    bottom_alpha = bottom[:, :, 3:]
    alpha = top_alpha + bottom_alpha * (1.0 - top_alpha)
    color = top[:, :, :3] * top_alpha + bottom[:, :, :3] * bottom_alpha * (1.0 - top_alpha)
    safe_alpha = np.where(alpha > 0, alpha, 1.0)
    return np.concatenate([color / safe_alpha, alpha], axis=2)


def _paint(buf: oiio.ImageBuf, coverage: np.ndarray, color: Color, left: int = 0, top: int = 0) -> oiio.ImageBuf:
    """Paint ``color`` over ``buf`` where ``coverage`` (height x width, 0-1, placed at left, top) is set.

    The result gains color if the paint has color, and keeps ``buf``'s pixel type and its alpha if it has one.
    """
    paint = _color_float(color)
    pixels = _rgba_float(buf)
    placement = _placed(pixels.shape, coverage.shape, left, top)
    if placement is not None:
        layer = np.empty((*coverage[placement.source].shape, RGBA_CHANNELS), dtype=np.float32)
        layer[:, :] = paint
        layer[:, :, 3] *= coverage[placement.source]
        pixels[placement.target] = _over(layer, pixels[placement.target])
    channels = _layout_with(buf.nchannels, color=_is_color(paint), alpha=False)
    return _from_rgba_float(pixels, channels, buf.spec().format)


def _placed(canvas_shape: tuple[int, ...], layer_shape: tuple[int, ...], left: int, top: int) -> _Placement | None:
    """Return where a layer at (left, top) lands on the canvas, or None if it misses."""
    canvas_height, canvas_width = canvas_shape[:2]
    layer_height, layer_width = layer_shape[:2]
    x0 = max(left, 0)
    y0 = max(top, 0)
    x1 = min(left + layer_width, canvas_width)
    y1 = min(top + layer_height, canvas_height)
    if x0 >= x1 or y0 >= y1:
        return None
    return _Placement(
        target=(slice(y0, y1), slice(x0, x1)), source=(slice(y0 - top, y1 - top), slice(x0 - left, x1 - left))
    )


def composited(buf: oiio.ImageBuf, overlay: oiio.ImageBuf, step: Composite) -> oiio.ImageBuf:
    """Draw ``overlay`` over ``buf`` as ``step`` says, in float, then store it in ``buf``'s pixel type."""
    base = _rgba_float(buf)
    layer = _rgba_float(overlay)
    layer[:, :, 3] *= np.float32(np.clip(step.opacity, 0.0, 1.0))
    placement = _placed(base.shape, layer.shape, step.x, step.y)
    if placement is not None:
        base[placement.target] = _over(layer[placement.source], base[placement.target])
    channels = _layout_with(buf.nchannels, color=overlay.nchannels >= RGB_CHANNELS, alpha=False)
    return _from_rgba_float(_clipped(base, buf), channels, buf.spec().format)


def with_alpha(buf: oiio.ImageBuf, alpha: oiio.ImageBuf | float) -> oiio.ImageBuf:
    """Return ``buf`` with its alpha replaced by a constant or a same-size buffer's gray value.

    Raises:
        ValueError: If the alpha image's size differs from the image's
    """
    spec = buf.spec()
    if isinstance(alpha, oiio.ImageBuf):
        alpha_spec = alpha.spec()
        if (alpha_spec.width, alpha_spec.height) != (spec.width, spec.height):
            msg = (
                f"Attempted to use a {alpha_spec.width}x{alpha_spec.height} image as the alpha of a "
                f"{spec.width}x{spec.height} image. Failed because they must be the same size."
            )
            raise ValueError(msg)
        values = float_pixels(to_gray(alpha))[:, :, 0]
    else:
        values = np.full((spec.height, spec.width), np.clip(alpha, 0.0, 1.0), dtype=np.float32)
    pixels = _rgba_float(buf)
    pixels[:, :, 3] = values
    channels = _layout_with(buf.nchannels, color=False, alpha=True)
    return _from_rgba_float(pixels, channels, spec.format)


# ---------------------------------------------------------------------------
# Geometry


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
        return buffer_from_float(_clipped(pixels, buf), buf.spec().format)
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
    fill = _color_float(step.fill)
    pixels = _rgba_float(buf)
    canvas = np.empty(
        (spec.height + step.top + step.bottom, spec.width + step.left + step.right, RGBA_CHANNELS), dtype=np.float32
    )
    canvas[:, :] = fill
    canvas[step.top : step.top + spec.height, step.left : step.left + spec.width] = pixels
    channels = _layout_with(buf.nchannels, color=_is_color(fill), alpha=fill[3] < 1.0)
    return _from_rgba_float(canvas, channels, spec.format)


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
        fill_color = _color_float(fill)
        fill_premultiplied = np.append(fill_color[:3] * fill_color[3], fill_color[3])
        premultiplied = premultiplied + fill_premultiplied * (1.0 - coverage)
        adds_alpha = has_alpha or fill_color[3] < 1.0
        adds_color = _is_color(fill_color)
    pixels = _unpremultiplied(premultiplied, _brightest(buf))
    channels = _layout_with(_without_alpha(buf.nchannels), color=adds_color, alpha=adds_alpha)
    return _from_rgba_float(_clipped(pixels, buf), channels, spec.format)


def _brightest(buf: oiio.ImageBuf) -> float:
    """The brightest color value in ``buf``, at least 1, so HDR highlights survive unpremultiplying."""
    color = float_pixels(buf)[:, :, : _color_channels(buf)]
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


# ---------------------------------------------------------------------------
# Color


def enhanced(buf: oiio.ImageBuf, step: Brightness | Contrast | Saturation) -> oiio.ImageBuf:
    """Blend color toward or away from black, the mean gray, or each pixel's gray, as Pillow's ``ImageEnhance``."""
    pixels = float_pixels(buf)
    color_channels = _color_channels(buf)
    color = pixels[:, :, :color_channels]
    factor = np.float32(step.factor)
    if isinstance(step, Brightness):
        color = color * factor
    else:
        gray = _luma(color)
        if isinstance(step, Contrast):
            gray = np.full_like(gray, gray.mean())
        color = gray + (color - gray) * factor
    pixels[:, :, :color_channels] = color
    return buffer_from_float(_clipped(pixels, buf), buf.spec().format)


def inverted(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Invert color, leaving alpha."""
    pixels = float_pixels(buf)
    color_channels = _color_channels(buf)
    pixels[:, :, :color_channels] = 1.0 - pixels[:, :, :color_channels]
    return buffer_from_float(pixels, buf.spec().format)


def _color_channels(buf: oiio.ImageBuf) -> int:
    alpha = alpha_channel(buf)
    if alpha is None:
        return buf.nchannels
    return alpha


def _luma(color: np.ndarray) -> np.ndarray:
    """Return each pixel's gray as a height x width x 1 array, for one- or three-channel color."""
    if color.shape[2] < RGB_CHANNELS:
        return color[:, :, :1].copy()
    return (color[:, :, :RGB_CHANNELS] @ _LUMA)[:, :, np.newaxis]


# ---------------------------------------------------------------------------
# Filters


def blurred(buf: oiio.ImageBuf, radius: float) -> oiio.ImageBuf:
    """Gaussian blur with standard deviation ``radius``, edges extended, in premultiplied float.

    Approximated as Pillow does, by three box blurs of a fractional radius, so the cost doesn't
    grow with the radius.
    """
    if radius <= 0:
        return buf.copy()
    box_radius = _box_radius(radius, _BOX_PASSES)
    has_alpha = alpha_channel(buf) is not None
    pixels = float_pixels(buf).astype(np.float64)
    if has_alpha:
        pixels[:, :, :-1] *= pixels[:, :, -1:]
    for axis in (1, 0):
        for _ in range(_BOX_PASSES):
            pixels = _box_blurred(pixels, box_radius, axis)
    if has_alpha:
        alpha = pixels[:, :, -1:]
        safe_alpha = np.where(alpha > 0, alpha, 1.0)
        pixels[:, :, :-1] = np.where(alpha > 0, pixels[:, :, :-1] / safe_alpha, 0.0)
    return buffer_from_float(_clipped(pixels.astype(np.float32), buf), buf.spec().format)


def _box_radius(sigma: float, passes: int) -> float:
    """The fractional box radius whose ``passes`` repeats have variance ``sigma`` squared, as Pillow computes it."""
    variance = sigma * sigma / passes
    ideal = math.sqrt(12 * variance + 1)
    whole = math.floor((ideal - 1) / 2)
    fraction = (2 * whole + 1) * (whole * (whole + 1) - 3 * variance)
    fraction /= 6 * (variance - (whole + 1) * (whole + 1))
    return whole + fraction


def _box_blurred(pixels: np.ndarray, radius: float, axis: int) -> np.ndarray:
    """Average along ``axis`` over a box of fractional ``radius``: whole pixels within it, the next ones in part."""
    whole = math.floor(radius)
    fraction = radius - whole
    reach = whole + 1
    pad = [(0, 0)] * pixels.ndim
    pad[axis] = (reach + 1, reach)
    padded_pixels = np.pad(pixels, pad, mode="edge")
    sums = np.cumsum(padded_pixels, axis=axis)
    length = pixels.shape[axis]

    def window(start: int) -> np.ndarray:
        return np.take(sums, np.arange(start, start + length), axis=axis)

    # Sum of the 2 * whole + 1 pixels centered on each, from the running sums
    inner = window(reach + 1 + whole) - window(reach - whole)
    outer = window(reach + 1 + reach) - window(reach - reach) - inner
    return (inner + fraction * outer) / (2 * radius + 1)


def _convolved(pixels: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolve float pixels with a 2-D kernel through OpenImageIO, unnormalized, zero outside."""
    kernel_buf = oiio.ImageBuf(np.ascontiguousarray(kernel[:, :, np.newaxis], dtype=np.float32))
    # Centered on the origin, as convolve expects
    kernel_buf.set_origin(-(kernel.shape[1] // 2), -(kernel.shape[0] // 2))
    source = oiio.ImageBuf(np.ascontiguousarray(pixels, dtype=np.float32))
    # Positional: the binding spells this keyword "normalze"
    result = checked(oiio.ImageBufAlgo.convolve(source, kernel_buf, False))
    return float_pixels(result)


def dilated(buf: oiio.ImageBuf, size: int) -> oiio.ImageBuf:
    """Take each channel's largest value in a ``size`` x ``size`` square, as Pillow's ``MaxFilter``.

    Raises:
        ValueError: If ``size`` isn't odd and positive
    """
    _check_filter_size(size)
    if size == 1:
        return buf.copy()
    return checked(oiio.ImageBufAlgo.dilate(buf, size, size))


def eroded(buf: oiio.ImageBuf, size: int) -> oiio.ImageBuf:
    """Take each channel's smallest value in a ``size`` x ``size`` square, as Pillow's ``MinFilter``.

    Raises:
        ValueError: If ``size`` isn't odd and positive
    """
    _check_filter_size(size)
    if size == 1:
        return buf.copy()
    return checked(oiio.ImageBufAlgo.erode(buf, size, size))


def _check_filter_size(size: int) -> None:
    """Raise ValueError unless ``size`` is odd and positive, so the square centers on each pixel, as Pillow requires."""
    if size < 1 or size % 2 == 0:
        msg = f"Attempted to filter an image with a {size}x{size} square. Failed because the size must be odd and positive."
        raise ValueError(msg)


def edges(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Pillow's ``FIND_EDGES`` on color, the 1-pixel border left as it was, as Pillow leaves it.

    Unlike Pillow, alpha is kept rather than filtered, so an opaque image doesn't turn transparent.
    """
    pixels = float_pixels(buf)
    color_channels = _color_channels(buf)
    filtered = _convolved(pixels[:, :, :color_channels], _FIND_EDGES_KERNEL)
    # Never negative, as Pillow clamps; float highlights keep their range
    pixels[1:-1, 1:-1, :color_channels] = np.maximum(filtered[1:-1, 1:-1], 0.0)
    return buffer_from_float(_clipped(pixels, buf), buf.spec().format)


# ---------------------------------------------------------------------------
# Drawing


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
    return _paint(buf, float_pixels(coverage)[:, :, 0], step.color, step.x, step.y)


def with_rectangle(buf: oiio.ImageBuf, step: DrawRectangle) -> oiio.ImageBuf:
    """Draw a box's fill, then its outline. An empty box draws nothing."""
    width = step.right - step.left
    height = step.bottom - step.top
    if width <= 0 or height <= 0:
        return buf.copy()
    result = buf
    if step.fill is not None:
        result = _paint(result, np.ones((height, width), dtype=np.float32), step.fill, step.left, step.top)
    if step.outline is not None and step.width > 0:
        ring = np.ones((height, width), dtype=np.float32)
        inset = step.width
        if height > 2 * inset and width > 2 * inset:
            ring[inset:-inset, inset:-inset] = 0.0
        result = _paint(result, ring, step.outline, step.left, step.top)
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
    return _paint(buf, inside.astype(np.float32), step.color, left, top)
