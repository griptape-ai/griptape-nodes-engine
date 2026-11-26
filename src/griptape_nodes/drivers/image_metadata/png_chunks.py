"""PNG text extraction and rules shared by the Pillow and encoded-byte metadata paths.

Chunk rewriting edits PNG text without decoding the image, so pixel data such as
16-bit channels stays exactly as written.
"""

import struct
import zlib
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from PIL import PngImagePlugin

from griptape_nodes.files.path_utils import canonicalize_for_io

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Text longer than this, such as an embedded workflow, is stored compressed. Shorter text stays
# uncompressed, where image viewers that only show plain text chunks can still read it.
COMPRESS_TEXT_LONGER_THAN = 1024

MAX_KEYWORD_LENGTH = 79

_TEXT_CHUNK_TYPES = frozenset({b"tEXt", b"zTXt", b"iTXt"})
_CHUNK_HEADER_SIZE = 8
_CRC_SIZE = 4
_COPY_BLOCK_SIZE = 1 << 20
_IHDR_SIZE = 13
_ITXT_FLAGS_SIZE = 2
_ITXT_FIELD_COUNT = 3


def should_compress_text(text: str) -> bool:
    """Decide whether a text chunk can be compressed and still be read back.

    Pillow rejects a zTXt/iTXt chunk whose decompressed size exceeds
    PngImagePlugin.MAX_TEXT_CHUNK, raising ValueError from Image.open. Mirror the
    encoding add_text falls back to (latin-1, then utf-8) to check the size Pillow
    will actually decompress.
    """
    if len(text) <= COMPRESS_TEXT_LONGER_THAN:
        return False
    try:
        encoded_length = len(text.encode("latin-1"))
    except UnicodeEncodeError:
        encoded_length = len(text.encode("utf-8"))
    return encoded_length <= PngImagePlugin.MAX_TEXT_CHUNK


def read_png_text(path: Path) -> dict[str, str]:
    """Read all PNG text, including chunks after pixel data, without decoding pixels.

    Non-text payloads are skipped by seeking. Text uses PNG's Latin-1 or UTF-8 encoding,
    and duplicate keywords take the last value. Compressed values and total decoded text
    are bounded by Pillow's text limits. The source is never modified.

    Raises:
        OSError: If the PNG is invalid, incomplete, or cannot be read.
        ValueError: If text is malformed or exceeds Pillow's text memory limits.

    Non-text checksums and pixel contents are not validated.
    """
    with canonicalize_for_io(path).open("rb") as source:
        source.seek(0, 2)
        file_size = source.tell()
        source.seek(0)
        if source.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            msg = f"Attempted to read text from '{path.name}'. Failed because the file is not a PNG."
            raise OSError(msg)

        text: dict[str, str] = {}
        text_memory = 0
        seen_header = False
        seen_pixels = False
        while True:
            header = source.read(_CHUNK_HEADER_SIZE)
            if len(header) != _CHUNK_HEADER_SIZE:
                _raise_read_incomplete(path.name)
            length = struct.unpack(">I", header[:4])[0]
            kind = header[4:]
            chunk_end = source.tell() + length + _CRC_SIZE
            if chunk_end > file_size:
                _raise_read_incomplete(path.name)
            if not seen_header and (kind != b"IHDR" or length != _IHDR_SIZE):
                msg = f"Attempted to read text from '{path.name}'. Failed because the PNG header is invalid."
                raise OSError(msg)
            seen_header = True

            if kind in _TEXT_CHUNK_TYPES:
                decoded = _read_text_chunk(source, kind, length, path.name)
                # Count every chunk, including overwritten duplicate keywords, as Pillow does.
                text_memory += sum(len(value) for value in decoded.values())
                _check_text_memory(text_memory)
                text.update(decoded)
            else:
                source.seek(chunk_end)

            if kind == b"IDAT":
                seen_pixels = True
            if kind == b"IEND":
                if length != 0 or not seen_pixels:
                    msg = f"Attempted to read text from '{path.name}'. Failed because the PNG is incomplete."
                    raise OSError(msg)
                break

    return text


def _raise_read_incomplete(file_name: str) -> None:
    msg = f"Attempted to read text from '{file_name}'. Failed because the PNG is incomplete."
    raise OSError(msg)


def _check_text_memory(text_memory: int) -> None:
    if text_memory > PngImagePlugin.MAX_TEXT_MEMORY:
        msg = "Attempted to read PNG text. Failed because the total text exceeds the memory limit."
        raise ValueError(msg)


def _read_text_chunk(source: BinaryIO, kind: bytes, length: int, file_name: str) -> dict[str, str]:
    # Bound the encoded chunk too, before allocating its payload.
    if length > PngImagePlugin.MAX_TEXT_MEMORY:
        msg = "Attempted to read PNG text. Failed because a text chunk exceeds the memory limit."
        raise ValueError(msg)
    payload = source.read(length)
    checksum = source.read(_CRC_SIZE)
    if len(payload) != length or len(checksum) != _CRC_SIZE:
        _raise_read_incomplete(file_name)
    if struct.unpack(">I", checksum)[0] != zlib.crc32(kind + payload):
        msg = f"Attempted to read text from '{file_name}'. Failed because a text checksum is invalid."
        raise OSError(msg)
    keyword, separator, value = payload.partition(b"\x00")
    if not separator or not 1 <= len(keyword) <= MAX_KEYWORD_LENGTH:
        msg = "Attempted to read PNG text. Failed because a text keyword is invalid."
        raise ValueError(msg)
    return {keyword.decode("latin-1"): _decode_text_value(kind, value)}


def _decode_text_value(kind: bytes, value: bytes) -> str:
    if kind == b"tEXt":
        return value.decode("latin-1")
    if kind == b"zTXt":
        if not value or value[0] != 0:
            msg = "Attempted to read PNG text. Failed because the text compression method is invalid."
            raise ValueError(msg)
        return _decompress_text(value[1:]).decode("latin-1")

    if len(value) < _ITXT_FLAGS_SIZE or value[0] not in {0, 1} or value[1] != 0:
        msg = "Attempted to read PNG text. Failed because the international text compression flags are invalid."
        raise ValueError(msg)
    compressed = value[0] == 1
    fields = value[2:].split(b"\x00", 2)
    if len(fields) != _ITXT_FIELD_COUNT:
        msg = "Attempted to read PNG text. Failed because the international text fields are incomplete."
        raise ValueError(msg)
    language, translated_keyword, encoded = fields
    _decode_text_utf8(language)
    _decode_text_utf8(translated_keyword)
    if compressed:
        encoded = _decompress_text(encoded)
    return _decode_text_utf8(encoded)


def _decode_text_utf8(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "Attempted to read PNG text. Failed because the international text is not valid UTF-8."
        raise ValueError(msg) from exc
    return decoded


def _decompress_text(value: bytes) -> bytes:
    # Match Pillow's _safe_zlib_decompress bound, but also require a complete stream.
    # PngStream's text handlers silently discard malformed text, and Pillow's helper
    # does not check eof, so neither provides the strict read contract here.
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(value, PngImagePlugin.MAX_TEXT_CHUNK)
    except zlib.error as exc:
        msg = "Attempted to read PNG text. Failed because the compressed text is invalid."
        raise ValueError(msg) from exc
    if decompressor.unconsumed_tail:
        msg = "Attempted to read PNG text. Failed because the decompressed text exceeds the memory limit."
        raise ValueError(msg)
    if not decompressor.eof or decompressor.unused_data:
        msg = "Attempted to read PNG text. Failed because the compressed text is incomplete or invalid."
        raise ValueError(msg)
    return decoded


def rewrite_png_text_bytes(data: bytes, metadata: dict[str, str]) -> bytes:
    """Replace PNG text chunks without decoding or re-encoding the image bytes.

    Unrelated text and all non-text chunks stay byte-for-byte unchanged. Keys follow the
    PNG keyword rules; invalid or incomplete PNG data raises OSError.
    Empty metadata returns the original bytes without parsing them.
    """
    if not metadata:
        return data

    new_chunks = [_text_chunk(key, value) for key, value in metadata.items()]
    replaced_keys = {key.encode("latin-1") for key in metadata}
    with BytesIO(data) as source, BytesIO() as target:
        _stream_with_text(source, target, new_chunks, replaced_keys, "image data")
        return target.getvalue()


def _stream_with_text(
    source: BinaryIO, target: BinaryIO, new_chunks: list[bytes], replaced_keys: set[bytes], file_name: str
) -> None:
    if source.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        msg = f"Attempted to embed metadata in '{file_name}'. Failed because the file is not a PNG."
        raise OSError(msg)
    target.write(PNG_SIGNATURE)

    inserted = False
    while True:
        header = source.read(_CHUNK_HEADER_SIZE)
        if len(header) < _CHUNK_HEADER_SIZE:
            _raise_incomplete(file_name)
        length = struct.unpack(">I", header[:4])[0]
        kind = header[4:]

        if kind in _TEXT_CHUNK_TYPES:
            body = source.read(length + _CRC_SIZE)
            if len(body) < length + _CRC_SIZE:
                _raise_incomplete(file_name)
            if _keyword(body[:length]) not in replaced_keys:
                target.write(header)
                target.write(body)
        else:
            target.write(header)
            _copy_exactly(source, target, length + _CRC_SIZE, file_name)

        # The first chunk is IHDR. Text goes straight after it, ahead of the pixel data.
        if not inserted:
            target.writelines(new_chunks)
            inserted = True

        if kind == b"IEND":
            return


def _copy_exactly(source: BinaryIO, target: BinaryIO, count: int, file_name: str) -> None:
    remaining = count
    while remaining:
        block = source.read(min(remaining, _COPY_BLOCK_SIZE))
        if not block:
            _raise_incomplete(file_name)
        target.write(block)
        remaining -= len(block)


def _raise_incomplete(file_name: str) -> None:
    msg = f"Attempted to embed metadata in '{file_name}'. Failed because the PNG is incomplete."
    raise OSError(msg)


def _keyword(payload: bytes) -> bytes:
    """The keyword at the start of a tEXt, zTXt or iTXt payload."""
    return payload.split(b"\x00", 1)[0]


def _text_chunk(key: str, value: str) -> bytes:
    """Encode one key-value pair the way Pillow's `PngInfo.add_text` does.

    Latin-1 text becomes tEXt, or zTXt when long. Other text becomes iTXt, which holds UTF-8.
    """
    keyword = _encode_keyword(key)
    compress = should_compress_text(value)
    try:
        latin1 = value.encode("latin-1")
    except UnicodeEncodeError:
        utf8 = value.encode("utf-8")
        flag = b"\x00"
        if compress:
            utf8 = zlib.compress(utf8)
            flag = b"\x01"
        return _chunk(b"iTXt", keyword + b"\x00" + flag + b"\x00" + b"\x00" + b"\x00" + utf8)

    if compress:
        return _chunk(b"zTXt", keyword + b"\x00\x00" + zlib.compress(latin1))
    return _chunk(b"tEXt", keyword + b"\x00" + latin1)


def _encode_keyword(key: str) -> bytes:
    try:
        keyword = key.encode("latin-1")
    except UnicodeEncodeError:
        keyword = b""
    if not 1 <= len(keyword) <= MAX_KEYWORD_LENGTH or b"\x00" in keyword:
        msg = (
            f"Attempted to embed metadata key '{key}' in a PNG. "
            f"Failed because a keyword must be 1 to {MAX_KEYWORD_LENGTH} Latin-1 characters with no NUL."
        )
        raise ValueError(msg)
    return keyword


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
