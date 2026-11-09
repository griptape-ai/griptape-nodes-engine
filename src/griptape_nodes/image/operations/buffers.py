"""Buffers shared by the operations: channel counts and names, and conversion to and from NumPy arrays."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.pixel_type import PixelType

if TYPE_CHECKING:
    from collections.abc import Callable


GRAYSCALE_CHANNELS = 1
GRAYSCALE_ALPHA_CHANNELS = 2
RGB_CHANNELS = 3
RGBA_CHANNELS = 4

# Keeps alpha as stored in the file. OpenImageIO otherwise premultiplies on read and divides it
# back out on write, which rounds 8-bit color under partial alpha.
UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"

_CHANNEL_NAMES = {
    GRAYSCALE_CHANNELS: ("Y",),
    GRAYSCALE_ALPHA_CHANNELS: ("Y", "A"),
    RGB_CHANNELS: ("R", "G", "B"),
    RGBA_CHANNELS: ("R", "G", "B", "A"),
}


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
    buf.specmod().attribute(UNASSOCIATED_ALPHA, 1)


def is_float(buf: oiio.ImageBuf) -> bool:
    """Whether ``buf``'s pixels are floating point."""
    return str(buf.spec().format) in ("half", "float", "double")


def clipped(pixels: np.ndarray, buf: oiio.ImageBuf) -> np.ndarray:
    """Clip float pixels to 0-1 when they'll be stored in ``buf``'s integer type; float types keep the full range."""
    if is_float(buf):
        return pixels
    return np.clip(pixels, 0.0, 1.0)


def applied(buf: oiio.ImageBuf, function: Callable[[np.ndarray], np.ndarray]) -> oiio.ImageBuf:
    """Run ``function`` on the pixels as a NumPy array in their own type and wrap what it returns.

    Raises:
        ValueError: If the function doesn't return an array an image can hold
    """
    result = function(native_pixels(buf))
    if not isinstance(result, np.ndarray):
        msg = f"Attempted to apply a function to an image's pixels. Failed because it returned {type(result).__name__}, not an array."
        raise ValueError(msg)  # noqa: TRY004 - the function's result, not this call's argument
    PixelType.from_numpy(result.dtype)
    return buffer_from_pixels(result)


def color_channel_count(buf: oiio.ImageBuf) -> int:
    """The number of channels before alpha: 1 for gray, 3 for RGB."""
    alpha = alpha_channel(buf)
    if alpha is None:
        return buf.nchannels
    return alpha
