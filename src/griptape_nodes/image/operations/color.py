"""Color: transfer functions and brightness, contrast, saturation, and inversion."""

from __future__ import annotations

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations.buffers import (
    RGB_CHANNELS,
    alpha_channel,
    buffer_from_float,
    clipped,
    color_channel_count,
    float_pixels,
)
from griptape_nodes.image.pipeline import Brightness, Contrast, Saturation

# ITU-R 601 luma, the weights Pillow's "L" conversion uses
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# Below this, the sRGB transfer function is linear
_SRGB_LINEAR_LIMIT = 0.0031308


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


def scene_linear(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return display (sRGB) pixels as scene-linear float, the inverse of ``display_referred``, alpha unchanged.

    Raises:
        ValueError: If OpenImageIO cannot read the pixels
    """
    pixels = float_pixels(buf)
    encoded = np.clip(pixels, 0.0, 1.0)
    linear = np.where(
        encoded <= _SRGB_LINEAR_LIMIT * 12.92, encoded / 12.92, np.power((encoded + 0.055) / 1.055, 2.4)
    ).astype(np.float32)
    source_spec = buf.spec()
    alpha = alpha_channel(buf)
    if alpha is not None:
        linear[:, :, alpha] = pixels[:, :, alpha]
    out = oiio.ImageBuf(np.ascontiguousarray(linear))
    out.specmod().channelnames = source_spec.channelnames
    out.specmod().alpha_channel = source_spec.alpha_channel
    return out


def enhanced(buf: oiio.ImageBuf, step: Brightness | Contrast | Saturation) -> oiio.ImageBuf:
    """Blend color toward or away from black, the mean gray, or each pixel's gray, as Pillow's ``ImageEnhance``."""
    pixels = float_pixels(buf)
    color_channels = color_channel_count(buf)
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
    return buffer_from_float(clipped(pixels, buf), buf.spec().format)


def inverted(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Invert color, leaving alpha."""
    pixels = float_pixels(buf)
    color_channels = color_channel_count(buf)
    pixels[:, :, :color_channels] = 1.0 - pixels[:, :, :color_channels]
    return buffer_from_float(pixels, buf.spec().format)


def _luma(color: np.ndarray) -> np.ndarray:
    """Return each pixel's gray as a height x width x 1 array, for one- or three-channel color."""
    if color.shape[2] < RGB_CHANNELS:
        return color[:, :, :1].copy()
    return (color[:, :, :RGB_CHANNELS] @ LUMA)[:, :, np.newaxis]
