"""Sample images for tests: pixels built with NumPy, encoded and decoded through OpenImageIO."""

import struct
import tempfile
import zlib
from pathlib import Path

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.formats import sniff_image_format

FIXTURES = Path(__file__).parent / "fixtures"

# A real sRGB ICC profile, as color-managed tools embed it
SRGB_PROFILE = (FIXTURES / "srgb.icc").read_bytes()

RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)

_GRAYSCALE_ALPHA_CHANNELS = 2
_UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"


def solid(width: int, height: int, color: tuple[int, ...] | int = WHITE) -> np.ndarray:
    """A ``height`` x ``width`` uint8 array of one color. An int color makes a one-channel gray image."""
    if isinstance(color, int):
        return np.full((height, width, 1), color, dtype=np.uint8)
    return np.full((height, width, len(color)), color, dtype=np.uint8)


def noise(width: int, height: int, channels: int = 3, *, seed: int = 0) -> np.ndarray:
    """Random uint8 pixels, which compress poorly."""
    return np.random.default_rng(seed).integers(0, 256, size=(height, width, channels), dtype=np.uint8)


def encode(pixels: np.ndarray, extension: str, **attributes: object) -> bytes:
    """Encode ``pixels`` with OpenImageIO into the format ``extension`` names ("png", "jpg", ...).

    ``attributes`` become output spec attributes, so ``compression="jpeg:95"`` sets JPEG quality.
    ``icc_profile=...`` embeds a color profile.
    """
    buf = oiio.ImageBuf(np.ascontiguousarray(pixels))
    # The pixels are unassociated: color isn't premultiplied by alpha
    buf.specmod().attribute(_UNASSOCIATED_ALPHA, 1)
    if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS:
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    icc_profile = attributes.pop("icc_profile", None)
    if isinstance(icc_profile, bytes):
        buf.specmod().attribute(
            "ICCProfile", oiio.TypeDesc(f"uint8[{len(icc_profile)}]"), np.frombuffer(icc_profile, dtype=np.uint8)
        )
    for name, value in attributes.items():
        buf.specmod().attribute(name, value)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"image.{extension}"
        if not buf.write(str(path)):
            msg = f"Attempted to encode a sample {extension} image. Failed because: {buf.geterror()}"
            raise ValueError(msg)
        return path.read_bytes()


def decode(data: bytes) -> np.ndarray:
    """Decode ``data`` with OpenImageIO into a uint8 ``height`` x ``width`` x ``channels`` array, alpha unassociated."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"image{_suffix(data)}"
        path.write_bytes(data)
        config = oiio.ImageSpec()
        config.attribute(_UNASSOCIATED_ALPHA, 1)
        buf = oiio.ImageBuf(str(path), 0, 0, config)
        pixels = buf.get_pixels(oiio.UINT8)
        if not isinstance(pixels, np.ndarray):
            msg = f"Attempted to decode a sample image. Failed because: {buf.geterror()}"
            raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
        return pixels


def read_spec(data: bytes) -> oiio.ImageSpec:
    """Read ``data``'s header with OpenImageIO: dimensions, channels, and the metadata it parses."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"image{_suffix(data)}"
        path.write_bytes(data)
        image_input = oiio.ImageInput.open(str(path))
        if image_input is None:
            msg = f"Attempted to read a sample image. Failed because: {oiio.geterror()}"
            raise ValueError(msg)
        try:
            return oiio.ImageSpec(image_input.spec())
        finally:
            image_input.close()


def size_of(data: bytes) -> tuple[int, int]:
    """Width and height of encoded ``data``."""
    spec = read_spec(data)
    return (spec.width, spec.height)


def icc_profile_of(data: bytes) -> bytes | None:
    """The color profile OpenImageIO reads from ``data``, or None."""
    profile = read_spec(data).getattribute("ICCProfile")
    if profile is None or len(profile) == 0:
        return None
    return bytes(profile)


def palette_png(width: int, height: int, palette: list[tuple[int, int, int]] | None = None) -> bytes:
    """An 8-bit palette PNG with every pixel set to the first palette entry."""
    if palette is None:
        palette = [WHITE, BLUE]
    header = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    plte = b"".join(bytes(color) for color in palette)
    # Each row starts with filter type 0
    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"PLTE", plte)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data))


def _suffix(data: bytes) -> str:
    image_format = sniff_image_format(data)
    if image_format is None:
        return ""
    return f".{image_format.extension}"


# Bytes per value of the TIFF field types ``gray_tiff`` accepts
_TIFF_TYPE_BYTES = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1}


def gray_tiff(tags: dict[int, tuple[int, bytes]]) -> bytes:
    """A 1x1 8-bit gray, uncompressed, little-endian TIFF whose IFD0 also holds ``tags``.

    ``tags`` maps a tag number to its field type and packed value bytes. Built by hand, so it can
    carry tags OpenImageIO doesn't write, at any size.
    """
    pixel_offset = 8
    pixel = b"\x80\x00"
    ifd_offset = pixel_offset + len(pixel)
    entries = {
        0x0100: (3, struct.pack("<H", 1)),  # ImageWidth
        0x0101: (3, struct.pack("<H", 1)),  # ImageLength
        0x0102: (3, struct.pack("<H", 8)),  # BitsPerSample
        0x0103: (3, struct.pack("<H", 1)),  # Compression: none
        0x0106: (3, struct.pack("<H", 1)),  # PhotometricInterpretation: black is zero
        0x0111: (4, struct.pack("<I", pixel_offset)),  # StripOffsets
        0x0115: (3, struct.pack("<H", 1)),  # SamplesPerPixel
        0x0116: (3, struct.pack("<H", 1)),  # RowsPerStrip
        0x0117: (4, struct.pack("<I", 1)),  # StripByteCounts
        **tags,
    }
    values_offset = ifd_offset + 2 + 12 * len(entries) + 4
    table = bytearray(struct.pack("<H", len(entries)))
    values = bytearray()
    for tag in sorted(entries):
        field_type, value = entries[tag]
        if len(value) <= 4:  # noqa: PLR2004
            field = value.ljust(4, b"\x00")
        else:
            field = struct.pack("<I", values_offset + len(values))
            values += value + b"\x00" * (len(value) & 1)
        table += struct.pack("<HHI", tag, field_type, len(value) // _TIFF_TYPE_BYTES[field_type]) + field
    table += struct.pack("<I", 0)
    return b"II" + struct.pack("<HI", 42, ifd_offset) + pixel + bytes(table) + bytes(values)
