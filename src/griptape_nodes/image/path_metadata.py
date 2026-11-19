"""Read container metadata from files while seeking across compressed pixels."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from griptape_nodes.files.path_utils import canonicalize_for_io
from griptape_nodes.image.containers import read_exif, read_icc_profile
from griptape_nodes.image.exif import Exif, tiff_metadata_from_source
from griptape_nodes.image.formats import ImageFormat
from griptape_nodes.image.metadata import ImageMetadata
from griptape_nodes.image.metadata_reader import FileMetadataSource
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, read_text_chunks

if TYPE_CHECKING:
    from pathlib import Path

_MAX_METADATA_CHUNKS = 65536
_JPEG_FILL = 0xFF
_JPEG_SOS = 0xDA
_JPEG_LENGTH_BYTES = 2


def read_path_metadata(path: Path, image_format: ImageFormat) -> ImageMetadata:
    """Read bounded text, EXIF and ICC ranges without reading pixel payloads."""
    with canonicalize_for_io(path).open("rb", buffering=0) as handle:
        source = FileMetadataSource(handle)
        if image_format == ImageFormat.TIFF:
            parsed = tiff_metadata_from_source(source)
            return ImageMetadata(exif=parsed.exif, icc_profile=parsed.icc_profile)
        if image_format in {ImageFormat.PNG, ImageFormat.WEBP}:
            data = _chunk_metadata(source, image_format)
        else:
            data = _jpeg_metadata(source)
    block = read_exif(data, image_format)
    exif = None
    if block is not None:
        exif = Exif.from_bytes(block)
    text = {}
    if image_format == ImageFormat.PNG:
        text = read_text_chunks(data)
    return ImageMetadata(text=text, exif=exif, icc_profile=read_icc_profile(data, image_format))


def _chunk_metadata(source: FileMetadataSource, image_format: ImageFormat) -> bytes:
    is_png = image_format == ImageFormat.PNG
    position = 12
    prefix = b"RIFF\x00\x00\x00\x00WEBP"
    selected = {b"EXIF", b"ICCP"}
    if is_png:
        position = len(PNG_SIGNATURE)
        prefix = PNG_SIGNATURE
        selected = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP"}
    result = bytearray(prefix)
    for _ in range(_MAX_METADATA_CHUNKS):
        if position + 8 > source.size:
            break
        header = source.read_at(position, 8)
        if is_png:
            size = int.from_bytes(header[:4], "big")
            kind = header[4:]
            padding = 4
        else:
            kind = header[:4]
            size = int.from_bytes(header[4:], "little")
            padding = size & 1
        end = position + 8 + size + padding
        if end > source.size:
            msg = "Attempted to read image metadata. Failed because an image chunk is incomplete."
            raise ValueError(msg)
        if kind in selected:
            result += header + source.read_at(position + 8, size + padding)
        position = end
        if kind == b"IEND":
            break
    else:
        msg = "Attempted to read image metadata. Failed because the image has too many metadata chunks."
        raise ValueError(msg)
    return bytes(result)


def _jpeg_metadata(source: FileMetadataSource) -> bytes:
    result = bytearray(b"\xff\xd8")
    position = 2
    for _ in range(_MAX_METADATA_CHUNKS):
        if position + 2 > source.size:
            break
        marker = source.read_at(position, 2)
        if marker[0] != _JPEG_FILL or marker[1] == _JPEG_SOS:
            break
        if marker[1] == _JPEG_FILL:
            position += 1
            continue
        if marker[1] in {0x01, *range(0xD0, 0xD9)}:
            position += 2
            continue
        length_bytes = source.read_at(position + 2, 2)
        size = struct.unpack(">H", length_bytes)[0]
        if size < _JPEG_LENGTH_BYTES or position + 2 + size > source.size:
            msg = "Attempted to read JPEG metadata. Failed because an image segment is incomplete."
            raise ValueError(msg)
        if marker[1] in {0xE1, 0xE2}:
            result += marker + length_bytes + source.read_at(position + 4, size - 2)
        position += size + 2
    else:
        msg = "Attempted to read JPEG metadata. Failed because the image has too many metadata segments."
        raise ValueError(msg)
    return bytes(result)
