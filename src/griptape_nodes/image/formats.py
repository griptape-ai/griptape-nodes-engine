"""Image container formats, and magic-byte sniffing for the common ones."""

from __future__ import annotations

from enum import StrEnum

# The ISO BMFF brand sits at offset 8-12, the longest header inspected below.
SNIFF_MIN_HEADER_BYTES = 12


class ImageFormat(StrEnum):
    """An image container format.

    Members are ``str`` subclasses equal to their upper-case name, so ``ImageFormat.JPEG == "JPEG"``.
    Use ``from_name`` to turn a format name or file extension in any spelling into a member.
    """

    PNG = "PNG"
    JPEG = "JPEG"
    # A JPEG holding more than one frame, as some cameras write. Sniffing reports it as JPEG.
    MPO = "MPO"
    WEBP = "WEBP"
    TIFF = "TIFF"
    BMP = "BMP"
    GIF = "GIF"
    TGA = "TGA"
    EXR = "EXR"

    @property
    def extension(self) -> str:
        """The usual file extension, without a dot ("jpg" for JPEG)."""
        return _EXTENSIONS[self]

    @classmethod
    def from_name(cls, name: str) -> ImageFormat | None:
        """Return the format a name or file extension refers to ("jpg", ".JPEG", "tif", "targa"), or None."""
        upper = name.strip().lstrip(".").upper()
        upper = _ALIASES.get(upper, upper)
        if upper not in cls.__members__:
            return None
        return cls[upper]


_ALIASES = {"JPG": "JPEG", "JPE": "JPEG", "TIF": "TIFF", "TARGA": "TGA", "OPENEXR": "EXR"}

_EXTENSIONS = {
    ImageFormat.PNG: "png",
    ImageFormat.JPEG: "jpg",
    ImageFormat.MPO: "mpo",
    ImageFormat.WEBP: "webp",
    ImageFormat.TIFF: "tiff",
    ImageFormat.BMP: "bmp",
    ImageFormat.GIF: "gif",
    ImageFormat.TGA: "tga",
    ImageFormat.EXR: "exr",
}

# Sniffed extension -> format
_SNIFFED_FORMATS = {
    "png": ImageFormat.PNG,
    "jpg": ImageFormat.JPEG,
    "gif": ImageFormat.GIF,
    "webp": ImageFormat.WEBP,
    "bmp": ImageFormat.BMP,
    "tiff": ImageFormat.TIFF,
}


def sniff_image_extension(data: bytes) -> str | None:  # noqa: PLR0911
    """Return the file extension (``"png"``, ``"jpg"``, ...) the header of ``data`` identifies, or None.

    Pure prefix checks; no decoding or decompression-bomb scans, so this is safe to run on every
    byte write regardless of payload type.
    """
    if len(data) < SNIFF_MIN_HEADER_BYTES:
        return None
    head = data[:SNIFF_MIN_HEADER_BYTES]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def sniff_image_format(data: bytes) -> ImageFormat | None:
    """Return the format the header of ``data`` identifies, or None."""
    extension = sniff_image_extension(data)
    if extension is None:
        return None
    return _SNIFFED_FORMATS[extension]
