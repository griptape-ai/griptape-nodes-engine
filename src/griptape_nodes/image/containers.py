"""Read and replace the EXIF block of encoded JPEG, PNG, and WEBP bytes, without decoding pixels.

For backends whose libraries don't expose the raw EXIF block (OpenImageIO) or only expose it
after a full decode (OpenCV).
"""

import struct
from typing import NamedTuple

from griptape_nodes.image.formats import ImageFormat
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, png_chunk

EXIF_HEADER = b"Exif\x00\x00"

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_JPEG_APP0 = 0xE0
_JPEG_APP1 = 0xE1
_JPEG_SOS = 0xDA
_JPEG_FILL = 0xFF
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_JPEG_MAX_SEGMENT_BYTES = 0xFFFF
# SOF0-SOF15, less DHT (C4), JPG (C8), and DAC (CC), which share the range
_JPEG_START_OF_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

_WEBP_HEADER_BYTES = 12
_WEBP_VP8X_EXIF_FLAG = 0x08
_WEBP_VP8X_ALPHA_FLAG = 0x10


class _Segment(NamedTuple):
    """A JPEG segment or PNG/WEBP chunk: its marker or type, where it starts, and where its payload lies."""

    kind: int | bytes
    start: int
    payload_start: int
    payload_end: int


def read_exif(data: bytes, image_format: ImageFormat | None) -> bytes | None:
    """Return the TIFF-structured EXIF block from JPEG APP1, a PNG eXIf chunk, or a WEBP EXIF chunk."""
    if image_format in (ImageFormat.JPEG, ImageFormat.MPO):
        for segment in _jpeg_segments(data):
            payload = data[segment.payload_start : segment.payload_end]
            if segment.kind == _JPEG_APP1 and payload.startswith(EXIF_HEADER):
                return payload[len(EXIF_HEADER) :]
        return None
    if image_format == ImageFormat.PNG:
        for chunk in _png_chunks(data):
            if chunk.kind == b"eXIf":
                return data[chunk.payload_start : chunk.payload_end]
        return None
    if image_format == ImageFormat.WEBP:
        for chunk in _webp_chunks(data):
            if chunk.kind == b"EXIF":
                return data[chunk.payload_start : chunk.payload_end].removeprefix(EXIF_HEADER)
    return None


class ImageSize(NamedTuple):
    width: int
    height: int


def is_complete(data: bytes, image_format: ImageFormat) -> bool:
    """Whether a JPEG or PNG runs to its end marker (EOI, IEND), as one cut short in transit doesn't."""
    if image_format == ImageFormat.JPEG:
        return data.startswith(_JPEG_SOI) and data.rstrip(b"\x00").endswith(_JPEG_EOI)
    if image_format == ImageFormat.PNG:
        chunks = _png_chunks(data)
        return bool(chunks) and chunks[-1].kind == b"IEND" and chunks[-1].payload_end + 4 <= len(data)
    return False


def jpeg_size(data: bytes) -> ImageSize | None:
    """Return the dimensions a JPEG's start-of-frame header records, or None if there isn't one."""
    for segment in _jpeg_segments(data):
        if segment.kind in _JPEG_START_OF_FRAME_MARKERS and segment.payload_end - segment.payload_start >= 5:  # noqa: PLR2004
            height, width = struct.unpack(">HH", data[segment.payload_start + 1 : segment.payload_start + 5])
            return ImageSize(width, height)
    return None


def replace_jpeg_exif(encoded: bytes, exif: bytes | None) -> bytes:
    """Return a JPEG with its EXIF APP1 removed, and ``exif`` written after any JFIF APP0 if given.

    Raises:
        ValueError: If ``exif`` is larger than one JPEG segment holds
    """
    segment_payload = b""
    if exif is not None:
        segment_payload = EXIF_HEADER + exif
        if len(segment_payload) + 2 > _JPEG_MAX_SEGMENT_BYTES:
            msg = "Attempted to write EXIF into a JPEG. Failed because the EXIF block is larger than a JPEG segment holds."
            raise ValueError(msg)

    kept = bytearray(_JPEG_SOI)
    pending_exif = exif is not None
    position = len(_JPEG_SOI)
    for segment in _jpeg_segments(encoded):
        if pending_exif and segment.kind != _JPEG_APP0:
            kept += struct.pack(">BBH", 0xFF, _JPEG_APP1, len(segment_payload) + 2) + segment_payload
            pending_exif = False
        if segment.kind == _JPEG_SOS:
            break
        payload = encoded[segment.payload_start : segment.payload_end]
        if not (segment.kind == _JPEG_APP1 and payload.startswith(EXIF_HEADER)):
            kept += encoded[segment.start : segment.payload_end]
        position = segment.payload_end
    if pending_exif:
        kept += struct.pack(">BBH", 0xFF, _JPEG_APP1, len(segment_payload) + 2) + segment_payload
    # Everything from SOS on (scan data, EOI), or whatever follows if the walk stopped early
    kept += encoded[position:]
    return bytes(kept)


def replace_png_exif(encoded: bytes, exif: bytes | None, *, dropped_chunks: frozenset[bytes] = frozenset()) -> bytes:
    """Return a PNG with its eXIf chunk and any ``dropped_chunks`` removed, and ``exif`` written before IDAT if given."""
    kept = bytearray(PNG_SIGNATURE)
    pending_exif = exif
    for chunk in _png_chunks(encoded):
        if chunk.kind == b"IDAT" and pending_exif is not None:
            kept += png_chunk(b"eXIf", pending_exif)
            pending_exif = None
        if chunk.kind != b"eXIf" and chunk.kind not in dropped_chunks:
            kept += encoded[chunk.start : chunk.payload_end + 4]
    return bytes(kept)


def add_webp_exif(encoded: bytes, exif: bytes, *, width: int, height: int, has_alpha: bool) -> bytes:
    """Return a WEBP with an EXIF chunk, converting a simple WEBP to the extended (VP8X) layout it needs."""
    chunks = encoded[_WEBP_HEADER_BYTES:]
    if chunks[:4] == b"VP8X":
        flags = chunks[8] | _WEBP_VP8X_EXIF_FLAG
        chunks = chunks[:8] + bytes([flags]) + chunks[9:]
    else:
        flags = _WEBP_VP8X_EXIF_FLAG
        if has_alpha:
            flags |= _WEBP_VP8X_ALPHA_FLAG
        canvas = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        chunks = b"VP8X" + struct.pack("<I", len(canvas)) + canvas + chunks
    chunks += b"EXIF" + struct.pack("<I", len(exif)) + exif + b"\x00" * (len(exif) & 1)
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WEBP" + chunks


def _jpeg_segments(data: bytes) -> list[_Segment]:
    """Return each JPEG segment before SOS, then SOS. Fill bytes (repeated 0xFF) before a marker are skipped."""
    segments = []
    position = len(_JPEG_SOI)
    while position + 2 <= len(data) and data[position] == _JPEG_FILL:
        start = position
        while position + 2 < len(data) and data[position + 1] == _JPEG_FILL:
            position += 1
        marker = data[position + 1]
        if marker in _JPEG_STANDALONE_MARKERS:
            segments.append(_Segment(marker, start, position + 2, position + 2))
            position += 2
            continue
        if marker == _JPEG_SOS:
            segments.append(_Segment(marker, start, position + 2, position + 2))
            break
        if position + 4 > len(data):
            break
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        segments.append(_Segment(marker, start, position + 4, position + 2 + length))
        position += 2 + length
    return segments


def _png_chunks(data: bytes) -> list[_Segment]:
    chunks = []
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, chunk_type = struct.unpack(">I4s", data[position : position + 8])
        payload_start = position + 8
        chunks.append(_Segment(chunk_type, position, payload_start, payload_start + length))
        position = payload_start + length + 4
    return chunks


def _webp_chunks(data: bytes) -> list[_Segment]:
    chunks = []
    position = _WEBP_HEADER_BYTES
    while position + 8 <= len(data):
        chunk_type = data[position : position + 4]
        (length,) = struct.unpack("<I", data[position + 4 : position + 8])
        chunks.append(_Segment(chunk_type, position, position + 8, position + 8 + length))
        position += 8 + length + (length & 1)
    return chunks
