"""Read and replace metadata in encoded JPEG, PNG, WEBP, and TIFF bytes, without decoding pixels.

OpenImageIO exposes EXIF only as parsed attributes, and reading even those means opening the
file, so the raw blocks are read from and spliced into the container directly.
"""

import struct
import zlib
from typing import NamedTuple

from griptape_nodes.image.exif import EXIF_HEADER, tiff_extra_metadata_kinds, tiff_metadata
from griptape_nodes.image.formats import ImageFormat
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, png_chunk

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_JPEG_APP0 = 0xE0
_JPEG_APP1 = 0xE1
_JPEG_APP2 = 0xE2
_JPEG_APP13 = 0xED
_JPEG_SOS = 0xDA
_JPEG_FILL = 0xFF
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_JPEG_MAX_SEGMENT_BYTES = 0xFFFF

_JPEG_ICC_HEADER = b"ICC_PROFILE\x00"
# After the header, each ICC segment holds its sequence number and the segment count
_JPEG_ICC_SEQUENCE_BYTES = 2
_JPEG_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_JPEG_IPTC_HEADER = b"Photoshop 3.0\x00"
# iCCP: profile name, NUL, compression method, then the zlib stream
_PNG_ICC_MIN_BYTES = 3
# Real profiles run to a few hundred kilobytes. A larger one is treated as absent, so a small chunk
# can't expand to fill memory.
MAX_ICC_PROFILE_BYTES = 16 * 1024 * 1024

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


def read_icc_profile(data: bytes, image_format: ImageFormat | None) -> bytes | None:
    """Return the ICC profile from JPEG APP2 segments, a PNG iCCP chunk, a WEBP ICCP chunk, or a TIFF IFD0."""
    if image_format in (ImageFormat.JPEG, ImageFormat.MPO):
        return _jpeg_icc_profile(data)
    if image_format == ImageFormat.PNG:
        return _png_icc_profile(data)
    if image_format == ImageFormat.WEBP:
        for chunk in _webp_chunks(data):
            if chunk.kind == b"ICCP":
                return data[chunk.payload_start : chunk.payload_end] or None
        return None
    if image_format == ImageFormat.TIFF:
        return tiff_metadata(data).icc_profile
    return None


def read_extra_metadata_kinds(data: bytes, image_format: ImageFormat | None) -> list[str]:
    """Name the metadata a JPEG, WEBP, or TIFF holds besides EXIF and its color profile ("XMP", "IPTC", ...).

    ``ImageMetadata`` doesn't carry these, so re-encoding the pixels loses them.
    """
    kinds = []
    if image_format in (ImageFormat.JPEG, ImageFormat.MPO):
        for segment in _jpeg_segments(data):
            payload = data[segment.payload_start : segment.payload_end]
            if segment.kind == _JPEG_APP1 and payload.startswith(_JPEG_XMP_HEADER) and "XMP" not in kinds:
                kinds.append("XMP")
            if segment.kind == _JPEG_APP13 and payload.startswith(_JPEG_IPTC_HEADER) and "IPTC" not in kinds:
                kinds.append("IPTC")
    elif image_format == ImageFormat.WEBP and any(chunk.kind == b"XMP " for chunk in _webp_chunks(data)):
        kinds.append("XMP")
    elif image_format == ImageFormat.TIFF:
        kinds = tiff_extra_metadata_kinds(data)
    return kinds


def is_complete(data: bytes, image_format: ImageFormat) -> bool:
    """Whether a JPEG or PNG runs to its end marker (EOI, IEND), as one cut short in transit doesn't."""
    if image_format == ImageFormat.JPEG:
        return data.startswith(_JPEG_SOI) and data.rstrip(b"\x00").endswith(_JPEG_EOI)
    if image_format == ImageFormat.PNG:
        chunks = _png_chunks(data)
        return bool(chunks) and chunks[-1].kind == b"IEND" and chunks[-1].payload_end + 4 <= len(data)
    return False


def fits_jpeg_segment(exif: bytes) -> bool:
    """Whether an EXIF block fits the one APP1 segment a JPEG holds it in."""
    return len(EXIF_HEADER) + len(exif) + 2 <= _JPEG_MAX_SEGMENT_BYTES


def replace_jpeg_exif(encoded: bytes, exif: bytes | None) -> bytes:
    """Return a JPEG with its EXIF APP1 removed, and ``exif`` written after any JFIF APP0 if given.

    Raises:
        ValueError: If ``exif`` is larger than one JPEG segment holds
    """
    segment_payload = b""
    if exif is not None:
        segment_payload = EXIF_HEADER + exif
        if not fits_jpeg_segment(exif):
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


def _jpeg_icc_profile(data: bytes) -> bytes | None:
    """Join the profile a JPEG splits across APP2 segments, in their sequence order."""
    parts: dict[int, bytes] = {}
    for segment in _jpeg_segments(data):
        payload = data[segment.payload_start : segment.payload_end]
        if segment.kind != _JPEG_APP2 or not payload.startswith(_JPEG_ICC_HEADER):
            continue
        body = payload[len(_JPEG_ICC_HEADER) :]
        if len(body) < _JPEG_ICC_SEQUENCE_BYTES:
            continue
        parts[body[0]] = body[_JPEG_ICC_SEQUENCE_BYTES:]
    if not parts:
        return None
    return b"".join(parts[sequence] for sequence in sorted(parts))


def _png_icc_profile(data: bytes) -> bytes | None:
    for chunk in _png_chunks(data):
        if chunk.kind == b"IDAT":
            return None
        if chunk.kind != b"iCCP":
            continue
        payload = data[chunk.payload_start : chunk.payload_end]
        name_end = payload.find(b"\x00")
        if name_end < 0 or len(payload) < name_end + _PNG_ICC_MIN_BYTES:
            return None
        return _decompressed_profile(payload[name_end + 2 :])
    return None


def _decompressed_profile(data: bytes) -> bytes | None:
    """Decompress an iCCP profile, or return None if it's invalid or decompresses past ``MAX_ICC_PROFILE_BYTES``."""
    decompressor = zlib.decompressobj()
    try:
        profile = decompressor.decompress(data, MAX_ICC_PROFILE_BYTES)
    except zlib.error:
        return None
    if decompressor.unconsumed_tail or not profile:
        return None
    return profile


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
