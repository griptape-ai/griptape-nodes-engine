"""GriptapeImage backed by OpenImageIO."""

from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path
from typing import ClassVar

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.formats import sniff_image_format
from griptape_nodes.image.griptape_image import GriptapeImage, normalize_format_name
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, insert_text_chunks, png_chunk, read_text_chunks

_EXIF_HEADER = b"Exif\x00\x00"

_GRAYSCALE_CHANNELS = 1
_GRAYSCALE_ALPHA_CHANNELS = 2
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_ALPHA = 3

_JPEG_SOI = b"\xff\xd8"
_JPEG_APP1 = 0xE1
_JPEG_SOS = 0xDA
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_JPEG_MAX_SEGMENT_BYTES = 0xFFFF

# Chunks OpenImageIO adds to every PNG: its empty EXIF, and color tags claiming sRGB whatever the
# source was. Dropped so output matches the other backends: untagged, with only the EXIF it's given.
_PNG_DROPPED_CHUNKS = frozenset({b"eXIf", b"sRGB", b"gAMA", b"cHRM"})

_WEBP_VP8X_EXIF_FLAG = 0x08
_WEBP_VP8X_ALPHA_FLAG = 0x10

# Keeps alpha as stored in the file. OpenImageIO otherwise premultiplies on read and divides it
# back out on write, which rounds 8-bit color under partial alpha.
_UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"


class OpenImageIOImage(GriptapeImage):
    """GriptapeImage over an ``OpenImageIO.ImageBuf``.

    Pixels keep the source's bit depth and type (16-bit PNG and TIFF stay 16-bit, EXR stays half
    or float) in R, G, B(, A) order with unassociated alpha. Decoding is deferred until pixels or
    dimensions are first needed, so reading only ``text_metadata`` or ``exif_bytes`` never decodes.

    OpenImageIO's Python bindings read and write files by name only, so in-memory sources and
    encodes go through a temporary file. They also expose EXIF only as parsed attributes, so the
    raw block is read from and spliced into the container directly, for JPEG, PNG, and WEBP.
    """

    _ENCODE_EXTENSIONS: ClassVar[dict[str, str]] = {
        "PNG": ".png",
        "JPEG": ".jpg",
        "WEBP": ".webp",
        "TIFF": ".tif",
        "BMP": ".bmp",
        "GIF": ".gif",
        "TGA": ".tga",
        "EXR": ".exr",
    }

    # OpenImageIO format name -> format name as GriptapeImage.format reports it
    _FORMAT_NAMES: ClassVar[dict[str, str]] = {
        "jpeg": "JPEG",
        "targa": "TGA",
        "openexr": "EXR",
    }

    # Pillow's defaults, so previews come out the same size whichever backend wrote them
    _DEFAULT_QUALITY: ClassVar[dict[str, int]] = {"JPEG": 75, "WEBP": 80}

    def __init__(
        self,
        *,
        source: bytes | None,
        source_path: Path | None,
        buf: oiio.ImageBuf | None,
        image_format: str | None,
    ) -> None:
        self._source = source
        self._source_path = source_path
        self._buf = buf
        self._format = image_format

    @property
    def width(self) -> int:
        return self._decoded().spec().width

    @property
    def height(self) -> int:
        return self._decoded().spec().height

    @property
    def format(self) -> str | None:
        return self._format

    @property
    def channels(self) -> int:
        return self._decoded().nchannels

    @property
    def color_space(self) -> str:
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return "Grayscale"
        if channels == _GRAYSCALE_ALPHA_CHANNELS:
            return "Grayscale+Alpha"
        if channels == _RGBA_CHANNELS:
            return "RGBA"
        return "RGB"

    @property
    def bit_depth(self) -> int:
        return self._decoded().spec().format.size() * 8

    @property
    def text_metadata(self) -> dict[str, str]:
        if self._source is None or self._format != "PNG":
            return {}
        return read_text_chunks(self._source)

    @property
    def exif_bytes(self) -> bytes | None:
        if self._source is None:
            return None
        return _read_exif(self._source, self._format)

    def close(self) -> None:
        self._source = None
        self._source_path = None
        self._buf = None

    def exif_transposed(self) -> OpenImageIOImage:
        # reorient reads the Orientation attribute OpenImageIO parsed from the source's EXIF
        return self._derive(oiio.ImageBufAlgo.reorient(self._decoded()))

    def thumbnail(self, max_width: int, max_height: int) -> OpenImageIOImage:
        buf = self._decoded()
        spec = buf.spec()
        scale = min(max_width / spec.width, max_height / spec.height)
        if scale >= 1:
            return self._derive(buf.copy())
        width = max(1, round(spec.width * scale))
        height = max(1, round(spec.height * scale))
        roi = oiio.ROI(0, width, 0, height, 0, 1, 0, buf.nchannels)
        has_alpha = _alpha_channel(buf) is not None
        # Filtering unassociated color would bleed the color of transparent pixels into the edges
        source = oiio.ImageBufAlgo.premult(buf) if has_alpha else buf
        # resize widens the filter by the shrink factor, so a tent filter antialiases. Lanczos3
        # takes about 8x longer on a 4000x3000 source.
        resized = _checked(oiio.ImageBufAlgo.resize(source, "triangle", roi=roi))
        if has_alpha:
            resized = _checked(oiio.ImageBufAlgo.unpremult(resized))
        return self._derive(resized)

    def to_rgb(self) -> OpenImageIOImage:
        buf = self._decoded()
        if buf.nchannels in (_GRAYSCALE_CHANNELS, _GRAYSCALE_ALPHA_CHANNELS):
            order: tuple[int | float, ...] = (0, 0, 0)
        else:
            order = (0, 1, 2)
        return self._derive(_checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B"))))

    def to_rgba(self) -> OpenImageIOImage:
        buf = self._decoded()
        channels = buf.nchannels
        if channels == _RGBA_CHANNELS:
            return self._derive(buf.copy())
        if channels == _GRAYSCALE_CHANNELS:
            order: tuple[int | float, ...] = (0, 0, 0, 1.0)
        elif channels == _GRAYSCALE_ALPHA_CHANNELS:
            order = (0, 0, 0, 1)
        else:
            order = (0, 1, 2, 1.0)
        return self._derive(_checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B", "A"))))

    def with_rounded_corners(self, radius: int) -> OpenImageIOImage:
        rgba = self.to_rgba()._decoded()
        if radius <= 0:
            return self._derive(rgba)

        spec = rgba.spec()
        pixels = rgba.get_pixels(spec.format)
        if not isinstance(pixels, np.ndarray):
            msg = f"Attempted to round an image's corners. Failed because OpenImageIO could not read its pixels: {rgba.geterror()}"
            raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
        coverage = _rounded_rectangle_coverage(spec.width, spec.height, radius)
        alpha_max = _max_value(pixels.dtype)
        pixels[:, :, _ALPHA] = (coverage * alpha_max).astype(pixels.dtype)
        return self._derive(oiio.ImageBuf(pixels))

    def flattened(self, background: tuple[int, int, int]) -> OpenImageIOImage:
        rgba = self.to_rgba()._decoded()
        spec = rgba.spec()
        foreground = oiio.ImageBufAlgo.premult(rgba)
        red, green, blue = background
        canvas = oiio.ImageBufAlgo.fill(
            (red / 255.0, green / 255.0, blue / 255.0, 1.0),
            oiio.ROI(0, spec.width, 0, spec.height, 0, 1, 0, _RGBA_CHANNELS),
        )
        composited = _checked(oiio.ImageBufAlgo.over(foreground, canvas))
        rgb = _checked(oiio.ImageBufAlgo.channels(composited, (0, 1, 2), ("R", "G", "B")))
        # over computes in float; return to the source's pixel type
        out = oiio.ImageBuf(oiio.ImageSpec(spec.width, spec.height, _RGB_CHANNELS, spec.format))
        out.copy_pixels(rgb)
        return self._derive(out)

    def encode(
        self,
        image_format: str,
        *,
        quality: int | None = None,
        optimize: bool = False,
        exif_bytes: bytes | None = None,
        text_metadata: dict[str, str] | None = None,
    ) -> bytes:
        target = normalize_format_name(image_format)
        extension = self._ENCODE_EXTENSIONS.get(target)
        if extension is None:
            msg = f"Attempted to save an image as {target}. Failed because the OpenImageIO image backend cannot write that format."
            raise ValueError(msg)

        out = self._clean_copy(target, quality, optimize=optimize)
        with _temporary_path(extension) as path:
            if not out.write(str(path)):
                msg = f"Attempted to save an image as {target}. Failed because OpenImageIO could not encode it: {out.geterror()}"
                raise ValueError(msg)
            encoded = path.read_bytes()

        # OpenImageIO writes an empty EXIF block into PNG and JPEG
        if target in ("PNG", "JPEG"):
            encoded = _replace_exif(encoded, target, exif_bytes)
        elif target == "WEBP" and exif_bytes is not None:
            encoded = _add_webp_exif(encoded, out.spec(), exif_bytes)
        if text_metadata and target == "PNG":
            encoded = insert_text_chunks(encoded, text_metadata)
        return encoded

    def _clean_copy(self, target: str, quality: int | None, *, optimize: bool) -> oiio.ImageBuf:
        """Copy the pixels into a buffer with no metadata, so only what ``encode`` is given gets written."""
        buf = self._decoded()
        if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS and target != "PNG":
            # PNG is the only format here that holds gray with alpha
            buf = self.to_rgba()._decoded()
        source_spec = buf.spec()
        spec = oiio.ImageSpec(source_spec.width, source_spec.height, buf.nchannels, source_spec.format)
        spec.channelnames = source_spec.channelnames
        spec.attribute(_UNASSOCIATED_ALPHA, 1)
        if quality is None:
            quality = self._DEFAULT_QUALITY.get(target)
        if target == "JPEG" and quality is not None:
            spec.attribute("compression", f"jpeg:{quality}")
        elif target == "WEBP" and quality is not None:
            spec.attribute("compression", f"webp:{quality}")
        elif target == "PNG" and optimize:
            spec.attribute("compression", "zip:9")

        out = oiio.ImageBuf(spec)
        out.copy_pixels(buf)
        return out

    def _decoded(self) -> oiio.ImageBuf:
        """Return the pixels, decoding the source on first use.

        Raises:
            ValueError: If the image was closed or OpenImageIO cannot decode the source
        """
        if self._buf is not None:
            return self._buf
        if self._source is None:
            msg = "Attempted to read an image's pixels. Failed because the image was closed."
            raise ValueError(msg)

        if self._source_path is not None:
            buf, file_format = _read_file(self._source_path)
        else:
            extension = self._ENCODE_EXTENSIONS.get(self._format or "", "")
            with _temporary_path(extension) as path:
                path.write_bytes(self._source)
                buf, file_format = _read_file(path)
        if self._format is None:
            self._format = self._FORMAT_NAMES.get(file_format, file_format.upper())
        self._buf = buf
        return buf

    def _derive(self, buf: oiio.ImageBuf) -> OpenImageIOImage:
        # The source's metadata, Orientation included, may no longer describe the pixels
        for attribute in [attribute.name for attribute in buf.spec().extra_attribs]:
            buf.specmod().erase_attribute(attribute)
        return OpenImageIOImage(source=None, source_path=None, buf=buf, image_format=self._format)

    @classmethod
    def open(cls, source: bytes | Path) -> OpenImageIOImage:
        """Open an image without decoding it. Pixels decode on first use.

        A source whose header isn't one of the common formats ``sniff_image_format`` knows (TGA,
        EXR, ...) is decoded here, to learn its format.

        Raises:
            OSError: If the file cannot be read
            ValueError: If the data is not an image OpenImageIO can read
        """
        if isinstance(source, bytes):
            data, path = source, None
        else:
            data, path = source.read_bytes(), source
        image = cls(source=data, source_path=path, buf=None, image_format=sniff_image_format(data))
        if image.format is None:
            image._decoded()
        return image


class _temporary_path:  # noqa: N801
    """A closed temporary file path, removed on exit. Closed so OpenImageIO can open it on Windows."""

    def __init__(self, suffix: str) -> None:
        self._suffix = suffix
        self._path: Path | None = None

    def __enter__(self) -> Path:
        handle, name = tempfile.mkstemp(suffix=self._suffix, prefix="gtn-image-")
        os.close(handle)
        self._path = Path(name)
        return self._path

    def __exit__(self, *_exc: object) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)


def _read_file(path: Path) -> tuple[oiio.ImageBuf, str]:
    config = oiio.ImageSpec()
    config.attribute(_UNASSOCIATED_ALPHA, 1)
    buf = oiio.ImageBuf(str(path), 0, 0, config)
    # force pulls the pixels into memory, so the file can go away
    if not buf.read(0, 0, force=True) or buf.has_error:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
        raise ValueError(msg)
    file_format = buf.file_format_name
    if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS:
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    # Formats that store associated alpha (EXR, some TIFF) ignore the config; unassociate them so
    # every buffer this backend holds is unassociated
    if _alpha_channel(buf) is not None and buf.spec().get_int_attribute(_UNASSOCIATED_ALPHA, 0) != 1:
        buf = _checked(oiio.ImageBufAlgo.unpremult(buf))
    return buf, file_format


def _alpha_channel(buf: oiio.ImageBuf) -> int | None:
    alpha = buf.spec().alpha_channel
    return alpha if alpha >= 0 else None


def _checked(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    if buf.has_error:
        msg = f"Attempted to transform an image. Failed because OpenImageIO reported: {buf.geterror()}"
        raise ValueError(msg)
    return buf


def _max_value(dtype: np.dtype) -> float:
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def _rounded_rectangle_coverage(width: int, height: int, radius: int) -> np.ndarray:
    """Return per-pixel coverage (0-1) of a ``width`` x ``height`` rectangle with rounded corners."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    # Distance from each pixel to the nearest point of the rectangle inset by radius
    nearest_x = np.clip(xs, radius, width - 1 - radius)
    nearest_y = np.clip(ys, radius, height - 1 - radius)
    distance = np.hypot(xs - nearest_x, ys - nearest_y)
    return np.clip(radius + 0.5 - distance, 0.0, 1.0)


def _read_exif(data: bytes, image_format: str | None) -> bytes | None:
    """Return the TIFF-structured EXIF block from JPEG APP1, a PNG eXIf chunk, or a WEBP EXIF chunk."""
    if image_format == "JPEG":
        for marker, start, end in _jpeg_segments(data):
            if marker == _JPEG_APP1 and data[start:end].startswith(_EXIF_HEADER):
                return data[start + len(_EXIF_HEADER) : end]
        return None
    if image_format == "PNG":
        for chunk_type, start, end in _png_chunks(data):
            if chunk_type == b"eXIf":
                return data[start:end]
        return None
    if image_format == "WEBP":
        position = 12
        while position + 8 <= len(data):
            chunk_type = data[position : position + 4]
            (length,) = struct.unpack("<I", data[position + 4 : position + 8])
            if chunk_type == b"EXIF":
                return data[position + 8 : position + 8 + length].removeprefix(_EXIF_HEADER)
            position += 8 + length + (length & 1)
    return None


def _replace_exif(encoded: bytes, image_format: str, exif: bytes | None) -> bytes:
    """Return ``encoded`` with its EXIF block removed, and ``exif`` written in its place if given."""
    if image_format == "JPEG":
        kept = bytearray(_JPEG_SOI)
        if exif is not None:
            payload = _EXIF_HEADER + exif
            if len(payload) + 2 > _JPEG_MAX_SEGMENT_BYTES:
                msg = "Attempted to write EXIF into a JPEG. Failed because the EXIF block is larger than a JPEG segment holds."
                raise ValueError(msg)
            kept += struct.pack(">BBH", 0xFF, _JPEG_APP1, len(payload) + 2) + payload
        position = len(_JPEG_SOI)
        for marker, start, end in _jpeg_segments(encoded):
            if marker == _JPEG_SOS:
                kept += encoded[position:]
                break
            if not (marker == _JPEG_APP1 and encoded[start:end].startswith(_EXIF_HEADER)):
                kept += encoded[position:end]
            position = end
        return bytes(kept)

    kept = bytearray(PNG_SIGNATURE)
    for chunk_type, start, end in _png_chunks(encoded):
        if chunk_type == b"IDAT" and exif is not None:
            kept += png_chunk(b"eXIf", exif)
            exif = None
        if chunk_type not in _PNG_DROPPED_CHUNKS:
            kept += encoded[start - 8 : end + 4]
    return bytes(kept)


def _add_webp_exif(encoded: bytes, spec: oiio.ImageSpec, exif: bytes) -> bytes:
    """Return ``encoded`` with an EXIF chunk, converting a simple WEBP to the extended (VP8X) layout it needs."""
    chunks = encoded[12:]
    if chunks[:4] == b"VP8X":
        flags = chunks[8] | _WEBP_VP8X_EXIF_FLAG
        chunks = chunks[:8] + bytes([flags]) + chunks[9:]
    else:
        flags = _WEBP_VP8X_EXIF_FLAG
        if spec.nchannels == _RGBA_CHANNELS:
            flags |= _WEBP_VP8X_ALPHA_FLAG
        canvas = (
            bytes([flags, 0, 0, 0]) + (spec.width - 1).to_bytes(3, "little") + (spec.height - 1).to_bytes(3, "little")
        )
        chunks = b"VP8X" + struct.pack("<I", len(canvas)) + canvas + chunks
    chunks += b"EXIF" + struct.pack("<I", len(exif)) + exif + b"\x00" * (len(exif) & 1)
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WEBP" + chunks


def _jpeg_segments(data: bytes) -> list[tuple[int, int, int]]:
    """Return (marker, payload start, payload end) for each JPEG segment up to and including SOS."""
    segments = []
    position = len(_JPEG_SOI)
    while position + 4 <= len(data) and data[position] == 0xFF:  # noqa: PLR2004
        marker = data[position + 1]
        if marker in _JPEG_STANDALONE_MARKERS:
            segments.append((marker, position + 2, position + 2))
            position += 2
            continue
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        segments.append((marker, position + 4, position + 2 + length))
        if marker == _JPEG_SOS:
            break
        position += 2 + length
    return segments


def _png_chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    """Return (chunk type, data start, data end) for each PNG chunk."""
    chunks = []
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, chunk_type = struct.unpack(">I4s", data[position : position + 8])
        start = position + 8
        chunks.append((chunk_type, start, start + length))
        position = start + length + 4
    return chunks
