"""Magic-byte sniffing for common image formats."""

# The ISO BMFF brand sits at offset 8-12, the longest header inspected below.
SNIFF_MIN_HEADER_BYTES = 12

# Sniffed extension -> format name as GriptapeImage.format reports it
_FORMAT_NAMES = {
    "png": "PNG",
    "jpg": "JPEG",
    "gif": "GIF",
    "webp": "WEBP",
    "bmp": "BMP",
    "tiff": "TIFF",
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


def sniff_image_format(data: bytes) -> str | None:
    """Return the format name (``"PNG"``, ``"JPEG"``, ...) the header of ``data`` identifies, or None."""
    extension = sniff_image_extension(data)
    if extension is None:
        return None
    return _FORMAT_NAMES[extension]
