"""Image container formats and magic-byte sniffing."""

from __future__ import annotations

from enum import StrEnum

# Longest header inspected below (RIFF....WEBP and the 12-byte JPEG 2000 signature box).
SNIFF_MIN_HEADER_BYTES = 12

_ASCII_WHITESPACE = frozenset(b" \t\r\n")
_SGI_VALID_STORAGE = frozenset({0, 1})
_SGI_VALID_BPC = frozenset({1, 2})


class ImageFormat(StrEnum):
    """An image container format supported by the engine.

    Members are ``str`` subclasses equal to their upper-case name, so ``ImageFormat.JPEG == "JPEG"``.
    Use ``from_name`` to turn a format name or file extension in any spelling into a member.
    """

    PNG = "PNG"
    JPEG = "JPEG"
    # Multi-picture JPEG written by cameras; magic-byte sniffing reports JPEG.
    MPO = "MPO"
    WEBP = "WEBP"
    TIFF = "TIFF"
    BMP = "BMP"
    GIF = "GIF"
    TGA = "TGA"
    EXR = "EXR"
    HDR = "HDR"
    DPX = "DPX"
    CIN = "CIN"
    PSD = "PSD"
    JP2 = "JP2"
    PFM = "PFM"
    PPM = "PPM"
    PGM = "PGM"
    PBM = "PBM"
    SGI = "SGI"
    DDS = "DDS"

    @property
    def extension(self) -> str:
        """Primary lowercase file extension without a leading dot (``"jpg"`` for JPEG)."""
        return _EXTENSIONS[self]

    @classmethod
    def from_name(cls, name: str) -> ImageFormat | None:
        """Return the format a name or file extension refers to (``"jpg"``, ``".JPEG"``, ``"openexr"``), or None."""
        upper = name.strip().lstrip(".").upper()
        upper = _ALIASES.get(upper, upper)
        if upper not in cls.__members__:
            return None
        return cls[upper]


_ALIASES = {
    "JPG": "JPEG",
    "JPE": "JPEG",
    "JFIF": "JPEG",
    "TIF": "TIFF",
    "TARGA": "TGA",
    "OPENEXR": "EXR",
    "RGBE": "HDR",
    "CINEON": "CIN",
    "JPEG2000": "JP2",
    "J2K": "JP2",
    "PNM": "PPM",
}

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
    ImageFormat.HDR: "hdr",
    ImageFormat.DPX: "dpx",
    ImageFormat.CIN: "cin",
    ImageFormat.PSD: "psd",
    ImageFormat.JP2: "jp2",
    ImageFormat.PFM: "pfm",
    ImageFormat.PPM: "ppm",
    ImageFormat.PGM: "pgm",
    ImageFormat.PBM: "pbm",
    ImageFormat.SGI: "sgi",
    ImageFormat.DDS: "dds",
}

# File extensions advertised by ImageArtifactProvider and the built-in image preview generators.
# Note: JPEG XL (.jxl) is excluded until OpenImageIO's PyPI wheel libjxl reader passes signature
# verification on standard .jxl files.
SUPPORTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "bmp",
        "webp",
        "tiff",
        "tif",
        "tga",
        "exr",
        "hdr",
        "dpx",
        "cin",
        "psd",
        "jp2",
        "pfm",
        "ppm",
        "pgm",
        "pbm",
        "sgi",
        "dds",
    }
)

_SNIFFED_FORMATS = {
    "png": ImageFormat.PNG,
    "jpg": ImageFormat.JPEG,
    "gif": ImageFormat.GIF,
    "webp": ImageFormat.WEBP,
    "bmp": ImageFormat.BMP,
    "tiff": ImageFormat.TIFF,
    "exr": ImageFormat.EXR,
    "hdr": ImageFormat.HDR,
    "dpx": ImageFormat.DPX,
    "cin": ImageFormat.CIN,
    "psd": ImageFormat.PSD,
    "jp2": ImageFormat.JP2,
    "pfm": ImageFormat.PFM,
    "ppm": ImageFormat.PPM,
    "pgm": ImageFormat.PGM,
    "pbm": ImageFormat.PBM,
    "sgi": ImageFormat.SGI,
    "dds": ImageFormat.DDS,
}

_NETPBM_MAGICS = {
    b"PF": "pfm",
    b"Pf": "pfm",
    b"P6": "ppm",
    b"P3": "ppm",
    b"P5": "pgm",
    b"P2": "pgm",
    b"P4": "pbm",
    b"P1": "pbm",
}


_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"v/1\x01", "exr"),
    (b"#?RADIANCE", "hdr"),
    (b"#?RGBE", "hdr"),
    (b"SDPX", "dpx"),
    (b"XPDS", "dpx"),
    (b"\x80\x2a\x5f\xd7", "cin"),
    (b"\xd7\x5f\x2a\x80", "cin"),
    (b"8BPS", "psd"),
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jp2"),
    (b"\xff\x4f\xff\x51", "jp2"),
    (b"DDS ", "dds"),
)


def sniff_image_extension(data: bytes) -> str | None:
    """Return the file extension (``"png"``, ``"jpg"``, ``"exr"``, ...) identified by ``data``'s header, or None.

    Pure prefix checks with no codec parsing or decompression scans, safe to run on every byte write.
    """
    if len(data) < SNIFF_MIN_HEADER_BYTES:
        return None
    head = data[:SNIFF_MIN_HEADER_BYTES]
    for magic, extension in _MAGIC_PREFIXES:
        if head.startswith(magic):
            return extension
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"\x01\xda" and head[2] in _SGI_VALID_STORAGE and head[3] in _SGI_VALID_BPC:
        return "sgi"
    netpbm_ext = _NETPBM_MAGICS.get(head[:2])
    if netpbm_ext is not None and head[2] in _ASCII_WHITESPACE:
        return netpbm_ext
    return None


def sniff_image_format(data: bytes) -> ImageFormat | None:
    """Return the format identified by ``data``'s header, or None."""
    extension = sniff_image_extension(data)
    if extension is None:
        return None
    return _SNIFFED_FORMATS[extension]
