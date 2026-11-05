"""Read and write PNG text chunks (tEXt, zTXt, iTXt) directly on encoded PNG bytes."""

import struct
import zlib
from collections.abc import Mapping
from typing import NamedTuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Text longer than this, such as an embedded workflow, is stored compressed. Shorter text stays
# uncompressed, where image viewers that only show plain text chunks can still read it.
COMPRESS_TEXT_LONGER_THAN = 1024

# Pillow refuses to read a compressed text chunk that decompresses past this many bytes
# (PngImagePlugin.MAX_TEXT_CHUNK), so text above it is written uncompressed.
MAX_COMPRESSED_TEXT_BYTES = 1024 * 1024

# Reading stops once the text read so far passes this, as Pillow does (PngImagePlugin.MAX_TEXT_MEMORY)
MAX_TOTAL_TEXT_BYTES = 64 * MAX_COMPRESSED_TEXT_BYTES

_CHUNK_HEADER_BYTES = 8
_CHUNK_CRC_BYTES = 4


class TextChunk(NamedTuple):
    key: str
    text: str


def should_compress_text(text: str) -> bool:
    """Decide whether a text chunk can be compressed and still be read back by Pillow.

    Mirrors the encoding the chunk is written with (latin-1, then utf-8) to check the size a
    reader will actually decompress.
    """
    if len(text) <= COMPRESS_TEXT_LONGER_THAN:
        return False
    try:
        encoded_length = len(text.encode("latin-1"))
    except UnicodeEncodeError:
        encoded_length = len(text.encode("utf-8"))
    return encoded_length <= MAX_COMPRESSED_TEXT_BYTES


def read_text_chunks(png: bytes) -> dict[str, str]:
    """Return every text chunk in ``png`` as key -> text. Later chunks win on duplicate keys.

    Returns an empty dict for data that is not a PNG. Chunks that fail to decode, or that
    decompress past ``MAX_COMPRESSED_TEXT_BYTES``, are skipped.
    """
    if not png.startswith(PNG_SIGNATURE):
        return {}

    texts: dict[str, str] = {}
    total_bytes = 0
    position = len(PNG_SIGNATURE)
    while position + _CHUNK_HEADER_BYTES <= len(png):
        length, chunk_type = struct.unpack(">I4s", png[position : position + _CHUNK_HEADER_BYTES])
        data_start = position + _CHUNK_HEADER_BYTES
        data = png[data_start : data_start + length]
        position = data_start + length + _CHUNK_CRC_BYTES
        if chunk_type == b"IEND":
            break
        chunk = _decode_text_chunk(chunk_type, data)
        if chunk is None:
            continue
        total_bytes += len(chunk.text)
        if total_bytes > MAX_TOTAL_TEXT_BYTES:
            break
        texts[chunk.key] = chunk.text
    return texts


def insert_text_chunks(png: bytes, texts: Mapping[str, str]) -> bytes:
    """Return ``png`` with a text chunk per entry of ``texts`` inserted before the first IDAT chunk.

    Text goes ahead of the pixel data because readers such as Pillow only report the text chunks
    they meet before IDAT until the image is fully loaded. Existing chunks are left in place, so a
    key already present appears twice.

    Raises:
        ValueError: If ``png`` is not a PNG or has no IDAT chunk
    """
    if not png.startswith(PNG_SIGNATURE):
        msg = "Attempted to add text chunks to image data. Failed because the data is not a PNG."
        raise ValueError(msg)

    position = len(PNG_SIGNATURE)
    while position + _CHUNK_HEADER_BYTES <= len(png):
        length, chunk_type = struct.unpack(">I4s", png[position : position + _CHUNK_HEADER_BYTES])
        if chunk_type == b"IDAT":
            chunks = b"".join(_encode_text_chunk(key, text) for key, text in texts.items())
            return png[:position] + chunks + png[position:]
        position += _CHUNK_HEADER_BYTES + length + _CHUNK_CRC_BYTES

    msg = "Attempted to add text chunks to image data. Failed because the PNG has no image data."
    raise ValueError(msg)


def _decode_text_chunk(chunk_type: bytes, data: bytes) -> TextChunk | None:
    try:
        if chunk_type == b"tEXt":
            key, _, text = data.partition(b"\x00")
            return TextChunk(key.decode("latin-1"), text.decode("latin-1"))
        if chunk_type == b"zTXt":
            key, _, rest = data.partition(b"\x00")
            # rest[0] is the compression method; 0 (zlib) is the only one defined
            return TextChunk(key.decode("latin-1"), _decompress_text(rest[1:]).decode("latin-1"))
        if chunk_type == b"iTXt":
            key, _, rest = data.partition(b"\x00")
            compressed = rest[0] == 1
            # Skip the compression method byte, then the language tag and translated keyword
            _, _, rest = rest[2:].partition(b"\x00")
            _, _, text = rest.partition(b"\x00")
            if compressed:
                text = _decompress_text(text)
            return TextChunk(key.decode("latin-1"), text.decode("utf-8"))
    except (UnicodeDecodeError, zlib.error, IndexError):
        return None
    return None


def _decompress_text(data: bytes) -> bytes:
    """Decompress a text chunk, stopping at the size limit so a small chunk cannot expand to fill memory.

    Raises:
        zlib.error: If the data is not valid zlib or decompresses past ``MAX_COMPRESSED_TEXT_BYTES``
    """
    decompressor = zlib.decompressobj()
    text = decompressor.decompress(data, MAX_COMPRESSED_TEXT_BYTES)
    if decompressor.unconsumed_tail:
        msg = "Attempted to read a PNG text chunk. Failed because it decompresses past the size limit."
        raise zlib.error(msg)
    return text


def _encode_text_chunk(key: str, text: str) -> bytes:
    """Encode one text chunk the way Pillow's PngInfo.add_text does: latin-1 if it fits, else iTXt."""
    compress = should_compress_text(text)
    key_bytes = key.encode("latin-1")
    try:
        text_bytes = text.encode("latin-1")
    except UnicodeEncodeError:
        utf8 = text.encode("utf-8")
        if compress:
            body = key_bytes + b"\x00\x01\x00\x00\x00" + zlib.compress(utf8)
        else:
            body = key_bytes + b"\x00\x00\x00\x00\x00" + utf8
        return png_chunk(b"iTXt", body)
    if compress:
        return png_chunk(b"zTXt", key_bytes + b"\x00\x00" + zlib.compress(text_bytes))
    return png_chunk(b"tEXt", key_bytes + b"\x00" + text_bytes)


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Return a complete PNG chunk: length, type, data, and CRC."""
    crc = zlib.crc32(chunk_type + data)
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)
