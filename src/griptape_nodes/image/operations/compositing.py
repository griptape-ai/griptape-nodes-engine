"""Compositing: layering images and paint, alpha, rounded corners, and flattening."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import (
    RGB_CHANNELS,
    RGBA_CHANNELS,
    checked,
    clipped,
    float_pixels,
    max_value,
)
from griptape_nodes.image.operations.channels import (
    color_float,
    from_rgba_float,
    is_color,
    layout_with,
    rgba_float,
    to_gray,
    to_rgba,
)

if TYPE_CHECKING:
    from griptape_nodes.image.pipeline import Color, Composite, Flatten, RoundCorners

_ALPHA = 3


class _Placement(NamedTuple):
    """Where a layer lands on a canvas: the canvas slices it covers and the matching layer slices."""

    target: tuple[slice, slice]
    source: tuple[slice, slice]


def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    """Porter-Duff ``top`` over ``bottom``, both float RGBA with unassociated alpha."""
    top_alpha = top[:, :, 3:]
    bottom_alpha = bottom[:, :, 3:]
    alpha = top_alpha + bottom_alpha * (1.0 - top_alpha)
    color = top[:, :, :3] * top_alpha + bottom[:, :, :3] * bottom_alpha * (1.0 - top_alpha)
    safe_alpha = np.where(alpha > 0, alpha, 1.0)
    return np.concatenate([color / safe_alpha, alpha], axis=2)


def painted(buf: oiio.ImageBuf, coverage: np.ndarray, color: Color, left: int = 0, top: int = 0) -> oiio.ImageBuf:
    """Paint ``color`` over ``buf`` where ``coverage`` (height x width, 0-1, placed at left, top) is set.

    The result gains color if the paint has color, and keeps ``buf``'s pixel type and its alpha if it has one.
    """
    paint = color_float(color)
    pixels = rgba_float(buf)
    placement = _placed(pixels.shape, coverage.shape, left, top)
    if placement is not None:
        layer = np.empty((*coverage[placement.source].shape, RGBA_CHANNELS), dtype=np.float32)
        layer[:, :] = paint
        layer[:, :, 3] *= coverage[placement.source]
        pixels[placement.target] = _over(layer, pixels[placement.target])
    channels = layout_with(buf.nchannels, color=is_color(paint), alpha=False)
    return from_rgba_float(pixels, channels, buf.spec().format)


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


def rounded(buf: oiio.ImageBuf, step: RoundCorners) -> oiio.ImageBuf:
    """Return RGBA with the alpha transparent outside a rectangle with rounded corners."""
    rgba = to_rgba(buf)
    spec = rgba.spec()
    radius = step.radius_pixels(spec.width, spec.height)
    if radius <= 0:
        return rgba

    pixels = rgba.get_pixels(spec.format)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to round an image's corners. Failed because OpenImageIO could not read its pixels: {rgba.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    coverage = _rounded_rectangle_coverage(spec.width, spec.height, radius)
    alpha_max = max_value(pixels.dtype)
    pixels[:, :, _ALPHA] = (coverage * alpha_max).astype(pixels.dtype)
    # An ImageBuf straight over the NumPy array, no file involved
    return oiio.ImageBuf(pixels)


def _rounded_rectangle_coverage(width: int, height: int, radius: int) -> np.ndarray:
    """Return per-pixel coverage (0-1) of a ``width`` x ``height`` rectangle with rounded corners."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    # Distance from each pixel to the nearest point of the rectangle inset by radius
    nearest_x = np.clip(xs, radius, width - 1 - radius)
    nearest_y = np.clip(ys, radius, height - 1 - radius)
    distance = np.hypot(xs - nearest_x, ys - nearest_y)
    return np.clip(radius + 0.5 - distance, 0.0, 1.0)


def flattened(buf: oiio.ImageBuf, step: Flatten) -> oiio.ImageBuf:
    """Return RGB composited over ``step``'s opaque background, in ``buf``'s pixel type."""
    rgba = to_rgba(buf)
    spec = rgba.spec()
    foreground = oiio.ImageBufAlgo.premult(rgba)
    red, green, blue = step.background
    canvas = oiio.ImageBufAlgo.fill(
        (red / 255.0, green / 255.0, blue / 255.0, 1.0),
        oiio.ROI(0, spec.width, 0, spec.height, 0, 1, 0, RGBA_CHANNELS),
    )
    composited = checked(oiio.ImageBufAlgo.over(foreground, canvas))
    rgb = checked(oiio.ImageBufAlgo.channels(composited, (0, 1, 2), ("R", "G", "B")))
    # over computes in float; return to the source's pixel type
    out = oiio.ImageBuf(oiio.ImageSpec(spec.width, spec.height, RGB_CHANNELS, spec.format))
    out.copy_pixels(rgb)
    return out


def composited(buf: oiio.ImageBuf, overlay: oiio.ImageBuf, step: Composite) -> oiio.ImageBuf:
    """Draw ``overlay`` over ``buf`` as ``step`` says, in float, then store it in ``buf``'s pixel type."""
    base = rgba_float(buf)
    layer = rgba_float(overlay)
    layer[:, :, 3] *= np.float32(np.clip(step.opacity, 0.0, 1.0))
    placement = _placed(base.shape, layer.shape, step.x, step.y)
    if placement is not None:
        base[placement.target] = _over(layer[placement.source], base[placement.target])
    channels = layout_with(buf.nchannels, color=overlay.nchannels >= RGB_CHANNELS, alpha=False)
    return from_rgba_float(clipped(base, buf), channels, buf.spec().format)


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
    pixels = rgba_float(buf)
    pixels[:, :, 3] = values
    channels = layout_with(buf.nchannels, color=False, alpha=True)
    return from_rgba_float(pixels, channels, spec.format)
