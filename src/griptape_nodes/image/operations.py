"""Pixel operations on OpenImageIO buffers, behind ``GriptapeImagePipeline`` steps and encoding.

Buffers hold unassociated alpha, last: gray (Y), gray and alpha (Y, A), RGB, or RGBA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813

if TYPE_CHECKING:
    from griptape_nodes.image.pipeline import Flatten, RoundCorners, Thumbnail

GRAYSCALE_CHANNELS = 1
GRAYSCALE_ALPHA_CHANNELS = 2
RGB_CHANNELS = 3
RGBA_CHANNELS = 4
_ALPHA = 3

# Shrink factor from which a thumbnail uses a box filter, about 4x faster than the tent filter. On
# photographic content they differ by about 1/255; detail near the pixel scale (fine stripes,
# halftone) can alias by tens of levels, which previews accept.
_BOX_FILTER_MIN_SHRINK = 3

# Below this, the sRGB transfer function is linear
_SRGB_LINEAR_LIMIT = 0.0031308


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


def is_float(buf: oiio.ImageBuf) -> bool:
    """Whether ``buf``'s pixels are floating point."""
    return str(buf.spec().format) in ("half", "float", "double")


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


def display_referred(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return scene-linear float pixels with the sRGB transfer function applied to color, clipped to 0-1.

    Raises:
        ValueError: If OpenImageIO cannot read the pixels
    """
    pixels = buf.get_pixels(oiio.FLOAT)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to prepare float pixels for display. Failed because OpenImageIO could not read them: {buf.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    linear = np.clip(pixels, 0.0, 1.0)
    encoded = np.where(linear <= _SRGB_LINEAR_LIMIT, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055).astype(
        np.float32
    )
    source_spec = buf.spec()
    alpha = alpha_channel(buf)
    if alpha is not None:
        encoded[:, :, alpha] = linear[:, :, alpha]
    out = oiio.ImageBuf(np.ascontiguousarray(encoded))
    out.specmod().channelnames = source_spec.channelnames
    out.specmod().alpha_channel = source_spec.alpha_channel
    return out


def _rounded_rectangle_coverage(width: int, height: int, radius: int) -> np.ndarray:
    """Return per-pixel coverage (0-1) of a ``width`` x ``height`` rectangle with rounded corners."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    # Distance from each pixel to the nearest point of the rectangle inset by radius
    nearest_x = np.clip(xs, radius, width - 1 - radius)
    nearest_y = np.clip(ys, radius, height - 1 - radius)
    distance = np.hypot(xs - nearest_x, ys - nearest_y)
    return np.clip(radius + 0.5 - distance, 0.0, 1.0)
