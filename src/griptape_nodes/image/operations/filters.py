"""Filters: Gaussian blur, dilation, erosion, and edge detection."""

from __future__ import annotations

import math

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import (
    alpha_channel,
    buffer_from_float,
    checked,
    clipped,
    color_channel_count,
    float_pixels,
)

# Pillow's ImageFilter.FIND_EDGES
_FIND_EDGES_KERNEL = np.array([[-1, -1, -1], [-1, 8, -1], [-1, -1, -1]], dtype=np.float32)

# Box blurs that approximate a Gaussian blur, as Pillow's ``GaussianBlur`` runs
_BOX_PASSES = 3


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
    return buffer_from_float(clipped(pixels.astype(np.float32), buf), buf.spec().format)


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
    color_channels = color_channel_count(buf)
    filtered = _convolved(pixels[:, :, :color_channels], _FIND_EDGES_KERNEL)
    # Never negative, as Pillow clamps; float highlights keep their range
    pixels[1:-1, 1:-1, :color_channels] = np.maximum(filtered[1:-1, 1:-1], 0.0)
    return buffer_from_float(clipped(pixels, buf), buf.spec().format)
