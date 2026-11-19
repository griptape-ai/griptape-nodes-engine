"""Encode decoded pixels to a format, and write image metadata into encoded bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.containers import (
    add_webp_exif,
    fits_jpeg_segment,
    read_icc_profile,
    replace_jpeg_exif,
    replace_png_exif,
)
from griptape_nodes.image.exif import replace_tiff_exif
from griptape_nodes.image.formats import ImageFormat
from griptape_nodes.image.image_io import ICC_PROFILE, UNASSOCIATED_ALPHA, temporary_path, write_file
from griptape_nodes.image.operations import (
    GRAYSCALE_ALPHA_CHANNELS,
    GRAYSCALE_CHANNELS,
    RGB_CHANNELS,
    RGBA_CHANNELS,
    alpha_channel,
    checked,
    display_referred,
    is_float,
    to_rgb,
    to_rgba,
)
from griptape_nodes.image.png_text_chunks import insert_text_chunks, replace_text_chunks

if TYPE_CHECKING:
    from griptape_nodes.image.exif import Exif
    from griptape_nodes.image.metadata import ImageMetadata

ENCODE_EXTENSIONS = {
    ImageFormat.PNG: ".png",
    ImageFormat.JPEG: ".jpg",
    ImageFormat.WEBP: ".webp",
    ImageFormat.TIFF: ".tif",
    ImageFormat.BMP: ".bmp",
    ImageFormat.GIF: ".gif",
    ImageFormat.TGA: ".tga",
    ImageFormat.EXR: ".exr",
    ImageFormat.HDR: ".hdr",
    ImageFormat.DPX: ".dpx",
    ImageFormat.JP2: ".jp2",
    ImageFormat.PFM: ".pfm",
    ImageFormat.PPM: ".ppm",
    ImageFormat.PGM: ".pgm",
    ImageFormat.PBM: ".pbm",
    ImageFormat.SGI: ".sgi",
}

# What each format holds of ``ImageMetadata``; ``GriptapeImage.encode`` warns about the rest
_TEXT_FORMATS = frozenset({ImageFormat.PNG})
_EXIF_FORMATS = frozenset({ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF})
_ICC_FORMATS = frozenset({ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF})

# Formats that store float pixels natively; the rest convert scene-linear floats to display sRGB
_FLOAT_FORMATS = frozenset({ImageFormat.EXR, ImageFormat.TIFF, ImageFormat.HDR, ImageFormat.PFM})

# Formats whose writers reject 1-channel grayscale buffers
_RGB_MINIMUM_FORMATS = frozenset({ImageFormat.WEBP, ImageFormat.GIF, ImageFormat.HDR, ImageFormat.PPM})

# Formats capable of storing more than 4 channels
_MULTI_CHANNEL_FORMATS = frozenset({ImageFormat.EXR, ImageFormat.TIFF})

# Quality when none is given. 75 is libjpeg's default. WEBP gets 80, a step above libwebp's 75, to
# keep fine detail in previews; at preview sizes the extra bytes are few.
_DEFAULT_QUALITY = {ImageFormat.JPEG: 75, ImageFormat.WEBP: 80}

# Chunks OpenImageIO adds to every PNG besides its empty EXIF: color tags claiming sRGB whatever the
# source was. Dropped so a PNG carries only the color profile and EXIF it was given.
_PNG_DROPPED_CHUNKS = frozenset({b"sRGB", b"gAMA", b"cHRM"})

# Bytes 16-20 of an ICC profile name the color space it describes
_ICC_COLOR_SPACE = slice(16, 20)
_ICC_GRAY = b"GRAY"
_ICC_RGB = b"RGB "

_TEXT_KIND = "text metadata"
_EXIF_KIND = "EXIF"
_ICC_KIND = "color profile"


def unwritable_kinds(image_format: ImageFormat, requested: ImageMetadata) -> list[str]:
    """Name the parts of ``requested`` that ``image_format`` has no place for."""
    dropped = []
    if requested.text and image_format not in _TEXT_FORMATS:
        dropped.append(_TEXT_KIND)
    if requested.exif and image_format not in _EXIF_FORMATS:
        dropped.append(_EXIF_KIND)
    if requested.icc_profile is not None and image_format not in _ICC_FORMATS:
        dropped.append(_ICC_KIND)
    return dropped


def reencoded(  # noqa: PLR0913
    buf: oiio.ImageBuf,
    image_format: ImageFormat,
    quality: int | None,
    requested: ImageMetadata,
    dropped: list[str],
    *,
    optimize: bool,
    holds_display_values: bool,
) -> bytes:
    """Encode ``buf`` with ``requested``'s color profile, then write the rest of it into the container."""
    _exif_block(requested.exif, image_format, dropped)
    buf = _output_pixels(buf, image_format, dropped)
    if buf.nchannels == GRAYSCALE_CHANNELS and image_format in _RGB_MINIMUM_FORMATS:
        buf = to_rgb(buf)
    elif buf.nchannels == GRAYSCALE_ALPHA_CHANNELS and image_format != ImageFormat.PNG:
        buf = to_rgba(buf)
    elif buf.nchannels > RGBA_CHANNELS and image_format not in _MULTI_CHANNEL_FORMATS:
        keep = (0, 1, 2) if image_format == ImageFormat.JPEG else (0, 1, 2, 3)
        names = ("R", "G", "B") if image_format == ImageFormat.JPEG else ("R", "G", "B", "A")
        buf = checked(oiio.ImageBufAlgo.channels(buf, keep, names))
    if is_float(buf) and image_format not in _FLOAT_FORMATS and not holds_display_values:
        buf = display_referred(buf)
    spec = _output_spec(buf, image_format, quality, optimize=optimize)
    icc_profile = None
    if requested.icc_profile is not None and image_format in _ICC_FORMATS:
        icc_profile = requested.icc_profile
    if icc_profile is not None and not icc_profile_fits(icc_profile, buf.nchannels):
        dropped.append(_ICC_KIND)
        icc_profile = None
    if icc_profile is not None:
        spec.attribute(
            ICC_PROFILE,
            oiio.TypeDesc(f"uint8[{len(icc_profile)}]"),
            np.frombuffer(icc_profile, dtype=np.uint8),
        )
    with temporary_path(ENCODE_EXTENSIONS[image_format]) as path:
        encoded = write_file(buf, spec, path)
    # The PNG writer skips a profile libpng rejects, without reporting it
    if icc_profile is not None and read_icc_profile(encoded, image_format) is None:
        dropped.append(_ICC_KIND)
    return _with_container_metadata(encoded, image_format, spec, requested, dropped)


def spliced(source: bytes, image_format: ImageFormat, metadata: ImageMetadata, dropped: list[str]) -> bytes:
    """Return a JPEG or PNG ``source`` with its EXIF and PNG text replaced by ``metadata``'s."""
    exif_block = _exif_block(metadata.exif, image_format, dropped)
    if image_format == ImageFormat.JPEG:
        return replace_jpeg_exif(source, exif_block)
    if image_format == ImageFormat.WEBP:
        with temporary_path(".webp") as path:
            path.write_bytes(source)
            header = oiio.ImageInput.open(str(path))
            if header is None:
                msg = "Attempted to update WebP metadata. Failed because the image header could not be read."
                raise ValueError(msg)
            try:
                spec = header.spec()
                return add_webp_exif(
                    source, exif_block, width=spec.width, height=spec.height, has_alpha=spec.nchannels == RGBA_CHANNELS
                )
            finally:
                header.close()
    encoded = replace_png_exif(source, exif_block)
    return replace_text_chunks(encoded, metadata.text)


def icc_profile_fits(icc_profile: bytes, channels: int) -> bool:
    """Whether a profile describes pixels with ``channels`` channels: RGB for color, gray for gray."""
    color_space = icc_profile[_ICC_COLOR_SPACE]
    if channels >= RGB_CHANNELS:
        return color_space == _ICC_RGB
    return color_space == _ICC_GRAY


def spoken_list(items: list[str]) -> str:
    """Join ``items`` as a sentence would: "a", "a and b", "a, b, and c"."""
    if len(items) <= 2:  # noqa: PLR2004
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _with_container_metadata(
    encoded: bytes,
    image_format: ImageFormat,
    spec: oiio.ImageSpec,
    requested: ImageMetadata,
    dropped: list[str],
) -> bytes:
    exif = None
    if requested.exif and image_format in _EXIF_FORMATS:
        exif = requested.exif
    exif_block = _exif_block(exif, image_format, dropped)
    if image_format == ImageFormat.PNG:
        encoded = replace_png_exif(encoded, exif_block, dropped_chunks=_PNG_DROPPED_CHUNKS)
    elif image_format == ImageFormat.JPEG:
        encoded = replace_jpeg_exif(encoded, exif_block)
    elif image_format == ImageFormat.WEBP and exif_block is not None:
        encoded = add_webp_exif(
            encoded,
            exif_block,
            width=spec.width,
            height=spec.height,
            has_alpha=spec.nchannels == RGBA_CHANNELS,
        )
    elif image_format == ImageFormat.TIFF and exif is not None:
        encoded = _tiff_with_exif(encoded, exif, dropped)
    if requested.text and image_format == ImageFormat.PNG:
        encoded = insert_text_chunks(encoded, requested.text)
    return encoded


def _tiff_with_exif(encoded: bytes, exif: Exif, dropped: list[str]) -> bytes:
    with_exif = replace_tiff_exif(encoded, exif)
    if with_exif is None:
        dropped.append(_EXIF_KIND)
        return encoded
    return with_exif


def _output_spec(
    buf: oiio.ImageBuf, image_format: ImageFormat, quality: int | None, *, optimize: bool
) -> oiio.ImageSpec:
    source_spec = buf.spec()
    spec = oiio.ImageSpec(source_spec.width, source_spec.height, buf.nchannels, source_spec.format)
    spec.channelnames = source_spec.channelnames
    spec.attribute(UNASSOCIATED_ALPHA, 1)
    if image_format == ImageFormat.TIFF:
        # OIIO 3.2.2 creates an EXIF directory for DateTime but cannot write it empty.
        # An unspecified color space supplies a valid EXIF entry without claiming sRGB.
        spec.attribute("Exif:ColorSpace", 65535)
    if quality is None:
        quality = _DEFAULT_QUALITY.get(image_format)
    if image_format == ImageFormat.JPEG and quality is not None:
        spec.attribute("compression", f"jpeg:{quality}")
    elif image_format == ImageFormat.WEBP and quality is not None:
        spec.attribute("compression", f"webp:{quality}")
        spec.attribute("webp:method", 0)
    elif image_format == ImageFormat.PNG:
        if optimize:
            spec.attribute("png:compressionLevel", 6)
        else:
            spec.attribute("png:compressionLevel", 1)
    return spec


def _output_pixels(buf: oiio.ImageBuf, image_format: ImageFormat, dropped: list[str]) -> oiio.ImageBuf:
    no_alpha_formats = {
        ImageFormat.JPEG,
        ImageFormat.HDR,
        ImageFormat.PFM,
        ImageFormat.PPM,
        ImageFormat.PGM,
        ImageFormat.PBM,
    }
    if image_format in no_alpha_formats and alpha_channel(buf) is not None:
        dropped.append("alpha")
        buf = to_rgb(buf)
    if image_format in {ImageFormat.PGM, ImageFormat.PBM} and buf.nchannels != GRAYSCALE_CHANNELS:
        buf = checked(oiio.ImageBufAlgo.channel_sum(buf, (0.2126, 0.7152, 0.0722)), buf)
    if image_format == ImageFormat.EXR and alpha_channel(buf) is not None:
        return checked(oiio.ImageBufAlgo.premult(buf), buf)
    return buf


def _exif_block(exif: Exif | None, image_format: ImageFormat, _dropped: list[str]) -> bytes | None:
    if not exif:
        return None
    block = exif.to_bytes()
    if image_format == ImageFormat.JPEG and not fits_jpeg_segment(block):
        msg = "Attempted to write image metadata to a JPEG. Failed because the metadata is too large for a JPEG file."
        raise ValueError(msg)
    return block
