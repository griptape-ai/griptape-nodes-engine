"""The image the engine decodes, transforms, and encodes images through, built on OpenImageIO."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NamedTuple, Self

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.containers import (
    add_webp_exif,
    fits_jpeg_segment,
    is_complete,
    read_exif,
    read_extra_metadata_kinds,
    read_icc_profile,
    replace_jpeg_exif,
    replace_png_exif,
)
from griptape_nodes.image.exif import (
    EXIF_ORIENTATION_TAG,
    EXIF_PIXEL_X_DIMENSION_TAG,
    EXIF_PIXEL_Y_DIMENSION_TAG,
    Exif,
    replace_tiff_exif,
    tiff_metadata,
)
from griptape_nodes.image.formats import ImageFormat, sniff_image_format
from griptape_nodes.image.metadata import ImageMetadata
from griptape_nodes.image.pipeline import Flatten, Orient, PipelineStep, RoundCorners, Thumbnail, ToRGB, ToRGBA
from griptape_nodes.image.png_text_chunks import insert_text_chunks, read_text_chunks

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

logger = logging.getLogger("griptape_nodes")

_GRAYSCALE_CHANNELS = 1
_GRAYSCALE_ALPHA_CHANNELS = 2
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_ALPHA = 3
_UPRIGHT = 1

# Chunks OpenImageIO adds to every PNG besides its empty EXIF: color tags claiming sRGB whatever the
# source was. Dropped so a PNG carries only the color profile and EXIF it's given.
_PNG_DROPPED_CHUNKS = frozenset({b"sRGB", b"gAMA", b"cHRM"})

# Text chunks, dropped from a source PNG whose metadata is rewritten in place, so only the text
# ``encode`` is given ends up in the output, as when it re-encodes
_PNG_TEXT_CHUNKS = frozenset({b"tEXt", b"zTXt", b"iTXt"})

# Shrink factor from which a thumbnail uses a box filter, about 4x faster than the tent filter. On
# photographic content they differ by about 1/255; detail near the pixel scale (fine stripes,
# halftone) can alias by tens of levels, which previews accept.
_BOX_FILTER_MIN_SHRINK = 3

# Keeps alpha as stored in the file. OpenImageIO otherwise premultiplies on read and divides it
# back out on write, which rounds 8-bit color under partial alpha.
_UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"

_ICC_PROFILE = "ICCProfile"
# Bytes 16-20 of an ICC profile name the color space it describes
_ICC_COLOR_SPACE = slice(16, 20)
_ICC_GRAY = b"GRAY"
_ICC_RGB = b"RGB "

# Below this, the sRGB transfer function is linear
_SRGB_LINEAR_LIMIT = 0.0031308

# Names for the parts of ``ImageMetadata`` in the warning ``encode`` logs when it drops some
_TEXT_KIND = "text metadata"
_EXIF_KIND = "EXIF"
_ICC_KIND = "color profile"


class PixelType(StrEnum):
    """The data type of one channel of one pixel."""

    UINT8 = "uint8"
    INT8 = "int8"
    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    HALF = "half"
    FLOAT = "float"
    DOUBLE = "double"

    @property
    def bits(self) -> int:
        """Bits per channel."""
        return _PIXEL_TYPE_BITS[self]

    @property
    def is_float(self) -> bool:
        """True for floating-point types, whose nominal range is 0-1 rather than the integer range."""
        return self in (PixelType.HALF, PixelType.FLOAT, PixelType.DOUBLE)

    @classmethod
    def from_numpy(cls, dtype: np.dtype) -> PixelType:
        """Return the pixel type of a NumPy array dtype.

        Raises:
            ValueError: If no pixel type matches ``dtype``
        """
        name = _NUMPY_PIXEL_TYPES.get(np.dtype(dtype).name)
        if name is None:
            msg = f"Attempted to read an image's pixel type. Failed because {dtype} is not a supported pixel type."
            raise ValueError(msg)
        return cls(name)


_PIXEL_TYPE_BITS = {
    PixelType.UINT8: 8,
    PixelType.INT8: 8,
    PixelType.UINT16: 16,
    PixelType.INT16: 16,
    PixelType.UINT32: 32,
    PixelType.INT32: 32,
    PixelType.HALF: 16,
    PixelType.FLOAT: 32,
    PixelType.DOUBLE: 64,
}

_NUMPY_PIXEL_TYPES = {
    "uint8": "uint8",
    "int8": "int8",
    "uint16": "uint16",
    "int16": "int16",
    "uint32": "uint32",
    "int32": "int32",
    "float16": "half",
    "float32": "float",
    "float64": "double",
}


class _Decoded(NamedTuple):
    buf: oiio.ImageBuf
    file_format_name: str


class _Header(NamedTuple):
    spec: oiio.ImageSpec
    file_format_name: str


class GriptapeImage:
    """A decoded image over an ``OpenImageIO.ImageBuf``.

    Instances are immutable. Open one with ``open`` and transform it with
    ``GriptapeImagePipeline``, which returns a new image carrying the source's metadata, updated to
    describe the new pixels. Use as a context manager, or call ``close``, to release the source
    early.

    Pixels keep the source's bit depth and type (16-bit PNG and TIFF stay 16-bit, EXR stays half
    or float) in R, G, B(, A) order with unassociated alpha. Palette images decode to full color.
    Decoding is deferred until pixels are first needed: dimensions, channels, pixel type, and
    ``metadata`` come from the file's header.

    Float pixels (EXR, float TIFF) with no color profile are taken as scene-linear, the EXR
    convention. Encoding them to a format that stores display values (PNG, JPEG, WEBP, ...)
    applies the sRGB transfer function, so they don't come out dark. Float pixels with a color
    profile are encoded as they are, since the profile describes their values, and so are the
    pixels a pipeline derives from them, profile or not.

    Pixel buffers stay in memory. Encoded bytes don't: reading or writing a file in memory needs
    ``IOProxy``, which the OpenImageIO Python bindings don't expose, so in-memory sources and
    encodes go through a temporary file. The bindings also expose EXIF only as parsed attributes,
    so the block is read from and spliced into the container directly.

    OpenImageIO 3.2 has no reduced-size JPEG decode, so a thumbnail of a large JPEG decodes the
    full image first.
    """

    _ENCODE_EXTENSIONS: ClassVar[dict[ImageFormat, str]] = {
        ImageFormat.PNG: ".png",
        ImageFormat.JPEG: ".jpg",
        ImageFormat.WEBP: ".webp",
        ImageFormat.TIFF: ".tif",
        ImageFormat.BMP: ".bmp",
        ImageFormat.GIF: ".gif",
        ImageFormat.TGA: ".tga",
        ImageFormat.EXR: ".exr",
    }

    # Formats whose source bytes can take new metadata without touching the pixel data
    _SPLICEABLE_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset({ImageFormat.PNG, ImageFormat.JPEG})

    # Formats whose EXIF and color profile are read straight from the container; the rest report
    # only the orientation and profile OpenImageIO parsed
    _CONTAINER_METADATA_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
        {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF}
    )

    # What each format holds of ``ImageMetadata``; ``encode`` warns about the rest
    _TEXT_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset({ImageFormat.PNG})
    _EXIF_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
        {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF}
    )
    _ICC_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
        {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF}
    )

    # OpenImageIO type name -> pixel type. 32-bit integers are "uint" and "int".
    _PIXEL_TYPES: ClassVar[dict[str, PixelType]] = {
        "uint8": PixelType.UINT8,
        "int8": PixelType.INT8,
        "uint16": PixelType.UINT16,
        "int16": PixelType.INT16,
        "uint": PixelType.UINT32,
        "int": PixelType.INT32,
        "half": PixelType.HALF,
        "float": PixelType.FLOAT,
        "double": PixelType.DOUBLE,
    }

    # Formats that store float pixels as they are; the rest get display values
    _FLOAT_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset({ImageFormat.EXR, ImageFormat.TIFF})

    # Quality when ``encode`` is given none. 75 is libjpeg's default. WEBP gets 80, a step above
    # libwebp's 75, to keep fine detail in previews; at preview sizes the extra bytes are few.
    _DEFAULT_QUALITY: ClassVar[dict[ImageFormat, int]] = {ImageFormat.JPEG: 75, ImageFormat.WEBP: 80}

    def __init__(  # noqa: PLR0913
        self,
        *,
        source: bytes | None,
        source_path: Path | None,
        buf: oiio.ImageBuf | None,
        image_format: ImageFormat | None,
        metadata: ImageMetadata | None = None,
        extra_metadata_kinds: list[str] | None = None,
        display_encoded: bool | None = None,
    ) -> None:
        """Wrap a source or pixel buffer. Use ``open`` to open an image.

        Args:
            source: Encoded bytes of an opened image, or None for one a pipeline produced
            source_path: The file ``source`` was read from, if any
            buf: Decoded pixels, or None to decode ``source`` on first use
            image_format: The container format, or None to learn it from the decode
            metadata: Metadata describing ``buf``, or None to read it from ``source`` on first use
            extra_metadata_kinds: Names of the metadata the original source held that
                ``ImageMetadata`` doesn't carry ("XMP", "IPTC"), or None to read them from ``source``
            display_encoded: Whether float pixels hold display values, as a color profile on the
                original source says, or None to decide from the image's own profile
        """
        self._source = source
        self._source_path = source_path
        self._buf = buf
        self._format = image_format
        self._metadata = metadata
        self._extra_kinds = extra_metadata_kinds
        self._display_encoded = display_encoded
        self._header: _Header | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def width(self) -> int:
        """Width in pixels."""
        return self._spec().width

    @property
    def height(self) -> int:
        """Height in pixels."""
        return self._spec().height

    @property
    def format(self) -> ImageFormat | None:
        """Source container format, or None if it is not one ``ImageFormat`` names."""
        return self._format

    @property
    def channels(self) -> int:
        """Number of channels per pixel, alpha included."""
        return self._spec().nchannels

    @property
    def color_space(self) -> str:
        """Human-readable color model: "RGB", "RGBA", "Grayscale", or "Grayscale+Alpha"."""
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return "Grayscale"
        if channels == _GRAYSCALE_ALPHA_CHANNELS:
            return "Grayscale+Alpha"
        if channels == _RGBA_CHANNELS:
            return "RGBA"
        return "RGB"

    @property
    def pixel_type(self) -> PixelType:
        """Data type of each channel of the pixels."""
        type_name = str(self._spec().format)
        pixel_type = self._PIXEL_TYPES.get(type_name)
        if pixel_type is None:
            msg = f"Attempted to read an image's pixel type. Failed because {type_name} is not a supported pixel type."
            raise ValueError(msg)
        return pixel_type

    @property
    def metadata(self) -> ImageMetadata:
        """The image's text, EXIF, and color profile. Empty once the image is closed.

        Returns a copy, so changing its EXIF leaves the image as it was.
        """
        if self._metadata is None:
            if self._source is None:
                return ImageMetadata()
            self._metadata = self._read_metadata(self._source)
        exif = None
        if self._metadata.exif is not None:
            exif = self._metadata.exif.copy()
        return replace(self._metadata, text=dict(self._metadata.text), exif=exif)

    def close(self) -> None:
        """Release the source and decoded pixels."""
        self._source = None
        self._source_path = None
        self._buf = None
        self._metadata = None
        self._extra_kinds = None
        self._header = None

    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,
        metadata: ImageMetadata | None = None,
    ) -> bytes:
        """Encode the image.

        Encoding an opened, complete JPEG or PNG back to its own format with no ``quality`` or
        ``optimize``, and the same color profile, splices the metadata into the source bytes
        without re-encoding the unchanged pixels. That keeps the source's other metadata (XMP,
        IPTC, comments), which a re-encode loses. That holds for an image with no metadata of its
        own too; only an explicit ``ImageMetadata()`` re-encodes, to leave out all metadata.

        Logs one warning naming what was dropped: parts of the metadata ``image_format`` has no
        place for, EXIF too large for a JPEG, and, when a re-encode writes the image's own
        metadata or any EXIF or text, the metadata ``ImageMetadata`` doesn't carry (XMP, IPTC)
        that the original source held. Metadata naming only a color profile, as previews pass,
        asks for nothing else, so losing the rest isn't reported.

        Args:
            image_format: Target format
            quality: Lossy quality 1-100 for JPEG/WEBP; None uses the format's default
            optimize: Spend extra time for a smaller file where the format supports it
            metadata: Metadata to write. None writes the image's own; ``ImageMetadata()`` writes none.
                PNG holds all of it; JPEG, WEBP, and TIFF hold EXIF and the color profile; BMP,
                GIF, TGA, and EXR hold none.

        Returns:
            The encoded file bytes

        Raises:
            ValueError: If the format can't be written or OpenImageIO fails to encode the pixels
        """
        extension = self._ENCODE_EXTENSIONS.get(image_format)
        if extension is None:
            msg = f"Attempted to save an image as {image_format}. Failed because that format can't be written."
            raise ValueError(msg)

        source = self._source
        own_metadata = metadata is None
        if metadata is None:
            requested = self.metadata
        else:
            requested = metadata
        dropped = self._unwritable_kinds(image_format, requested)
        # ``ImageMetadata()`` asks for no metadata at all, XMP and IPTC included, which only a re-encode drops
        writes_some = own_metadata or not requested.is_empty()
        if (
            source is not None
            and writes_some
            and self._can_splice(source, image_format, quality, requested, optimize=optimize)
        ):
            encoded = source
            if not own_metadata:
                encoded = _spliced(source, image_format, requested, dropped)
        else:
            encoded = self._reencoded(image_format, extension, quality, requested, dropped, optimize=optimize)
            if own_metadata or requested.exif or requested.text:
                dropped.extend(self._extra_metadata_kinds())
        if dropped:
            logger.warning("Saved an image as %s without its %s.", image_format, _spoken_list(dropped))
        return encoded

    def _can_splice(
        self,
        source: bytes,
        image_format: ImageFormat,
        quality: int | None,
        requested: ImageMetadata,
        *,
        optimize: bool,
    ) -> bool:
        """Whether ``encode`` can rewrite the source's metadata in place of re-encoding its pixels.

        Only an opened image has a source: derived images don't, so pixels a pipeline step changed
        always take the encoder. So does a source with no end marker (EOI, IEND) and a color
        profile other than the source's. The check is structural only: a source with a bad CRC or
        corrupt compressed data is spliced as is.
        """
        if image_format != self._format or image_format not in self._SPLICEABLE_FORMATS:
            return False
        if quality is not None or optimize:
            return False
        if requested.icc_profile != self.metadata.icc_profile:
            return False
        return is_complete(source, image_format)

    def _unwritable_kinds(self, image_format: ImageFormat, requested: ImageMetadata) -> list[str]:
        """Name the parts of ``requested`` that ``image_format`` has no place for."""
        dropped = []
        if requested.text and image_format not in self._TEXT_FORMATS:
            dropped.append(_TEXT_KIND)
        if requested.exif and image_format not in self._EXIF_FORMATS:
            dropped.append(_EXIF_KIND)
        if requested.icc_profile is not None and image_format not in self._ICC_FORMATS:
            dropped.append(_ICC_KIND)
        return dropped

    def _reencoded(  # noqa: PLR0913
        self,
        image_format: ImageFormat,
        extension: str,
        quality: int | None,
        requested: ImageMetadata,
        dropped: list[str],
        *,
        optimize: bool,
    ) -> bytes:
        """Encode the pixels with ``requested``'s color profile, then write the rest of it into the container.

        Appends what was lost to ``dropped``.
        """
        buf = self._decoded()
        if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS and image_format != ImageFormat.PNG:
            # PNG is the only format here that holds gray with alpha
            buf = _to_rgba(buf)
        if _is_float(buf) and image_format not in self._FLOAT_FORMATS and not self._holds_display_values():
            buf = _display_referred(buf)
        spec = self._output_spec(buf, image_format, quality, optimize=optimize)
        icc_profile = None
        if requested.icc_profile is not None and image_format in self._ICC_FORMATS:
            icc_profile = requested.icc_profile
        if icc_profile is not None and not _icc_profile_fits(icc_profile, buf.nchannels):
            dropped.append(_ICC_KIND)
            icc_profile = None
        if icc_profile is not None:
            spec.attribute(
                _ICC_PROFILE,
                oiio.TypeDesc(f"uint8[{len(icc_profile)}]"),
                np.frombuffer(icc_profile, dtype=np.uint8),
            )
        with _temporary_path(extension) as path:
            encoded = _write_file(buf, spec, path)
        # The PNG writer skips a profile libpng rejects, without reporting it
        if icc_profile is not None and read_icc_profile(encoded, image_format) is None:
            dropped.append(_ICC_KIND)
        return self._with_container_metadata(encoded, image_format, spec, requested, dropped)

    def _with_container_metadata(
        self,
        encoded: bytes,
        image_format: ImageFormat,
        spec: oiio.ImageSpec,
        requested: ImageMetadata,
        dropped: list[str],
    ) -> bytes:
        """Write ``requested``'s EXIF and text into freshly encoded bytes. Appends what was lost to ``dropped``."""
        exif = None
        if requested.exif and image_format in self._EXIF_FORMATS:
            exif = requested.exif
        exif_block = _exif_block(exif, image_format, dropped)
        # OpenImageIO writes an empty EXIF block into PNG and JPEG
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
                has_alpha=spec.nchannels == _RGBA_CHANNELS,
            )
        elif image_format == ImageFormat.TIFF and exif is not None:
            encoded = self._tiff_with_exif(encoded, exif, dropped)
        if requested.text and image_format == ImageFormat.PNG:
            encoded = insert_text_chunks(encoded, requested.text)
        return encoded

    def _tiff_with_exif(self, encoded: bytes, exif: Exif, dropped: list[str]) -> bytes:
        with_exif = replace_tiff_exif(encoded, exif)
        if with_exif is None:
            dropped.append(_EXIF_KIND)
            return encoded
        return with_exif

    def _output_spec(
        self, buf: oiio.ImageBuf, image_format: ImageFormat, quality: int | None, *, optimize: bool
    ) -> oiio.ImageSpec:
        """Return a spec for writing ``buf`` with no metadata, so only what ``encode`` is given gets written."""
        source_spec = buf.spec()
        spec = oiio.ImageSpec(source_spec.width, source_spec.height, buf.nchannels, source_spec.format)
        spec.channelnames = source_spec.channelnames
        spec.attribute(_UNASSOCIATED_ALPHA, 1)
        if quality is None:
            quality = self._DEFAULT_QUALITY.get(image_format)
        if image_format == ImageFormat.JPEG and quality is not None:
            spec.attribute("compression", f"jpeg:{quality}")
        elif image_format == ImageFormat.WEBP and quality is not None:
            spec.attribute("compression", f"webp:{quality}")
            # libwebp's fastest method. Higher ones search harder for a smaller file, and dominate
            # the cost at preview sizes.
            spec.attribute("webp:method", 0)
        elif image_format == ImageFormat.PNG:
            # The PNG writer ignores "compression". optimize spends time for size.
            if optimize:
                spec.attribute("png:compressionLevel", 6)
            else:
                spec.attribute("png:compressionLevel", 1)
        return spec

    def _holds_display_values(self) -> bool:
        """Whether float pixels hold display values rather than scene-linear ones.

        A color profile on the original source says they do: it describes the values as stored.
        Kept through pipeline steps that drop the profile, since they don't change what the values mean.
        """
        if self._display_encoded is None:
            return self.metadata.icc_profile is not None
        return self._display_encoded

    def _extra_metadata_kinds(self) -> list[str]:
        """Name the metadata the original source held that ``ImageMetadata`` doesn't carry ("XMP", "IPTC")."""
        if self._extra_kinds is None:
            if self._source is None:
                return []
            self._extra_kinds = read_extra_metadata_kinds(self._source, self._format)
        return list(self._extra_kinds)

    def _read_metadata(self, source: bytes) -> ImageMetadata:
        image_format = self._format
        text: dict[str, str] = {}
        if image_format == ImageFormat.PNG:
            text = read_text_chunks(source)
        if image_format == ImageFormat.TIFF:
            parsed = tiff_metadata(source)
            exif = None
            if parsed.exif:
                exif = parsed.exif
            return ImageMetadata(text=text, exif=exif, icc_profile=parsed.icc_profile)
        if image_format is not None and image_format in self._CONTAINER_METADATA_FORMATS:
            return ImageMetadata(
                text=text,
                exif=_container_exif(source, image_format),
                icc_profile=read_icc_profile(source, image_format),
            )
        spec = self._spec()
        exif = None
        orientation = spec.get_int_attribute("Orientation", _UPRIGHT)
        if orientation != _UPRIGHT:
            exif = Exif()
            exif[EXIF_ORIENTATION_TAG] = orientation
        return ImageMetadata(text=text, exif=exif, icc_profile=_icc_profile(spec))

    def _run_pipeline(self, steps: Sequence[PipelineStep]) -> GriptapeImage:
        """Apply ``steps`` in order and return the result. Intermediate images are closed as they're replaced."""
        current: GriptapeImage = self
        try:
            for step in steps:
                following = current._apply_step(step)
                if current is not self:
                    current.close()
                current = following
        except BaseException:
            if current is not self:
                current.close()
            raise
        if current is self:
            return self._apply_step(None)
        return current

    def _apply_step(self, step: PipelineStep | None) -> GriptapeImage:  # noqa: PLR0911
        """Return a new image with ``step`` applied, or an unchanged copy for None."""
        if step is None:
            return self._derive(self._decoded().copy(), step)
        if isinstance(step, Flatten):
            return self._derive(_flattened(self._decoded(), step), step)
        if isinstance(step, Thumbnail):
            return self._derive(self._thumbnail(step), step)
        if isinstance(step, Orient):
            return self._derive(_oriented(self._decoded(), step.orientation), step)
        if isinstance(step, ToRGB):
            return self._derive(_to_rgb(self._decoded()), step)
        if isinstance(step, ToRGBA):
            return self._derive(_to_rgba(self._decoded()), step)
        return self._derive(_rounded(self._decoded(), step), step)

    def _thumbnail(self, step: Thumbnail) -> oiio.ImageBuf:
        buf = self._decoded()
        spec = buf.spec()
        scale = min(step.max_width / spec.width, step.max_height / spec.height)
        if scale >= 1:
            return buf.copy()
        width = max(1, round(spec.width * scale))
        height = max(1, round(spec.height * scale))
        roi = oiio.ROI(0, width, 0, height, 0, 1, 0, buf.nchannels)
        has_alpha = _alpha_channel(buf) is not None
        # Filtering unassociated color would bleed the color of transparent pixels into the edges
        source = buf
        if has_alpha:
            source = oiio.ImageBufAlgo.premult(buf)
        # resize widens the filter by the shrink factor, so a tent filter antialiases. Lanczos3
        # takes about 8x longer on a 4000x3000 source.
        filter_name = "triangle"
        if scale * _BOX_FILTER_MIN_SHRINK <= 1:
            filter_name = "box"
        resized = _checked(oiio.ImageBufAlgo.resize(source, filter_name, roi=roi))
        if has_alpha:
            resized = _checked(oiio.ImageBufAlgo.unpremult(resized))
        return resized

    def _derive(self, buf: oiio.ImageBuf, step: PipelineStep | None) -> GriptapeImage:
        """Wrap a step's output, with this image's metadata updated to describe it."""
        metadata = self.metadata
        # OpenImageIO's copy of the source's metadata, Orientation included, isn't kept: what
        # ``encode`` writes comes from ``metadata``
        for attribute in [attribute.name for attribute in buf.spec().extra_attribs]:
            buf.specmod().erase_attribute(attribute)
        icc_profile = metadata.icc_profile
        # A new opaque canvas, composited in the pixels' own values, carries no profile
        if isinstance(step, Flatten):
            icc_profile = None
        if icc_profile is not None and not _icc_profile_fits(icc_profile, buf.nchannels):
            icc_profile = None
        exif = metadata.exif
        if exif is not None:
            _describe_pixels(exif, buf.spec(), oriented=isinstance(step, Orient))
        return type(self)(
            source=None,
            source_path=None,
            buf=buf,
            image_format=self._format,
            metadata=replace(metadata, exif=exif, icc_profile=icc_profile),
            extra_metadata_kinds=self._extra_metadata_kinds(),
            display_encoded=self._holds_display_values(),
        )

    def _spec(self) -> oiio.ImageSpec:
        """Return the decoded pixels' spec, or the source header's if the pixels aren't decoded yet.

        Raises:
            ValueError: If the image was closed or OpenImageIO cannot read the source's header
        """
        if self._buf is not None:
            return self._buf.spec()
        return self._read_header().spec

    def _read_header(self) -> _Header:
        """Return the source's header, reading it on first use, without decoding pixels.

        Raises:
            ValueError: If the image was closed or OpenImageIO cannot read the header
        """
        if self._header is not None:
            return self._header
        if self._source is None:
            msg = "Attempted to read an image's header. Failed because the image was closed."
            raise ValueError(msg)
        if self._source_path is not None:
            header = _read_header_file(self._source_path)
        else:
            with _temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                header = _read_header_file(path)
        self._header = header
        return header

    def _source_extension(self) -> str:
        if self._format is None:
            return ""
        return self._ENCODE_EXTENSIONS.get(self._format, "")

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
            decoded = _read_file(self._source_path)
        else:
            with _temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                decoded = _read_file(path)
        if self._format is None:
            self._format = ImageFormat.from_name(decoded.file_format_name)
        self._buf = decoded.buf
        return decoded.buf

    @classmethod
    def open(cls, source: bytes | Path) -> GriptapeImage:
        """Open an image from encoded bytes or a file path, without decoding it. Pixels decode on first use.

        A source whose header isn't one of the common formats ``sniff_image_format`` knows (TGA,
        EXR, ...) has its header read here, to learn its format. Otherwise a read error surfaces
        from the first call that needs the header or pixels.

        Raises:
            OSError: If the file cannot be read
            ValueError: If the data is not an image OpenImageIO can read
        """
        if isinstance(source, bytes):
            data = source
            path = None
        else:
            data = source.read_bytes()
            path = source
        image = cls(source=data, source_path=path, buf=None, image_format=sniff_image_format(data))
        if image.format is None:
            image._format = ImageFormat.from_name(image._read_header().file_format_name)
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


def _spoken_list(items: list[str]) -> str:
    """Join ``items`` as a sentence would: "a", "a and b", "a, b, and c"."""
    if len(items) <= 2:  # noqa: PLR2004
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _container_exif(source: bytes, image_format: ImageFormat) -> Exif | None:
    block = read_exif(source, image_format)
    if block is None:
        return None
    exif = Exif.from_bytes(block)
    if not exif:
        return None
    return exif


def _exif_block(exif: Exif | None, image_format: ImageFormat, dropped: list[str]) -> bytes | None:
    """Serialize ``exif`` to write into ``image_format``, or None if there's none or a JPEG can't hold it.

    Appends to ``dropped`` when a JPEG can't.
    """
    if not exif:
        return None
    block = exif.to_bytes()
    if image_format == ImageFormat.JPEG and not fits_jpeg_segment(block):
        dropped.append(_EXIF_KIND)
        return None
    return block


def _describe_pixels(exif: Exif, spec: oiio.ImageSpec, *, oriented: bool) -> None:
    """Update ``exif`` for pixels a pipeline step produced: upright if they were oriented, and their size."""
    if oriented:
        if EXIF_ORIENTATION_TAG in exif and exif.orientation != _UPRIGHT:
            exif[EXIF_ORIENTATION_TAG] = _UPRIGHT
        # The embedded thumbnail still faces the way the source did
        exif.remove_thumbnail()
    if EXIF_PIXEL_X_DIMENSION_TAG in exif:
        exif[EXIF_PIXEL_X_DIMENSION_TAG] = spec.width
    if EXIF_PIXEL_Y_DIMENSION_TAG in exif:
        exif[EXIF_PIXEL_Y_DIMENSION_TAG] = spec.height


def _icc_profile_fits(icc_profile: bytes, channels: int) -> bool:
    """Whether a profile describes pixels with ``channels`` channels: RGB for color, gray for gray.

    A profile for another color space (CMYK, Lab) describes the source's stored values, not the
    RGB they decode to.
    """
    color_space = icc_profile[_ICC_COLOR_SPACE]
    if channels >= _RGB_CHANNELS:
        return color_space == _ICC_RGB
    return color_space == _ICC_GRAY


def _read_header_file(path: Path) -> _Header:
    """Read the header of the image file at ``path``: dimensions, channels, pixel type, and metadata.

    Raises:
        ValueError: If OpenImageIO cannot read it
    """
    image_input = oiio.ImageInput.open(str(path))
    if image_input is None:
        msg = f"Attempted to read an image. Failed because OpenImageIO could not read it: {oiio.geterror()}"
        raise ValueError(msg)
    try:
        return _Header(oiio.ImageSpec(image_input.spec()), image_input.format_name())
    finally:
        image_input.close()


def _read_file(path: Path) -> _Decoded:
    config = oiio.ImageSpec()
    config.attribute(_UNASSOCIATED_ALPHA, 1)
    buf = oiio.ImageBuf(str(path), 0, 0, config)
    # force pulls the pixels into memory, so the file can go away
    if not buf.read(0, 0, force=True) or buf.has_error:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
        raise ValueError(msg)
    file_format_name = buf.file_format_name
    if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS:
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    # Formats that store associated alpha (EXR, some TIFF) ignore the config; unassociate them so
    # every buffer held is unassociated
    if _alpha_channel(buf) is not None and buf.spec().get_int_attribute(_UNASSOCIATED_ALPHA, 0) != 1:
        buf = _checked(oiio.ImageBufAlgo.unpremult(buf))
    return _Decoded(buf, file_format_name)


def _icc_profile(spec: oiio.ImageSpec) -> bytes | None:
    profile = spec.getattribute(_ICC_PROFILE)
    if profile is None or len(profile) == 0:
        return None
    return bytes(profile)


def _spliced(source: bytes, image_format: ImageFormat, metadata: ImageMetadata, dropped: list[str]) -> bytes:
    """Return a JPEG or PNG ``source`` with its EXIF and PNG text replaced by ``metadata``'s.

    Appends to ``dropped`` what wasn't written.
    """
    exif_block = _exif_block(metadata.exif, image_format, dropped)
    if image_format == ImageFormat.JPEG:
        return replace_jpeg_exif(source, exif_block)
    encoded = replace_png_exif(source, exif_block, dropped_chunks=_PNG_TEXT_CHUNKS)
    if metadata.text:
        encoded = insert_text_chunks(encoded, metadata.text)
    return encoded


def _write_file(buf: oiio.ImageBuf, spec: oiio.ImageSpec, path: Path) -> bytes:
    """Write ``buf``'s pixels to ``path`` under ``spec`` and return the file's bytes.

    Through an ``ImageOutput`` opened with ``spec``, so ``buf``'s own metadata isn't written and
    its pixels aren't copied into a metadata-free buffer first.

    Raises:
        ValueError: If OpenImageIO cannot encode the pixels
    """
    out = oiio.ImageOutput.create(str(path))
    if out is None:
        msg = (
            f"Attempted to save an image. Failed because OpenImageIO has no writer for {path.suffix}: {oiio.geterror()}"
        )
        raise ValueError(msg)
    try:
        if not out.open(str(path), spec) or not buf.write(out):
            msg = f"Attempted to save an image. Failed because OpenImageIO could not encode it: {out.geterror() or buf.geterror()}"
            raise ValueError(msg)
    finally:
        out.close()
    return path.read_bytes()


def _alpha_channel(buf: oiio.ImageBuf) -> int | None:
    alpha = buf.spec().alpha_channel
    if alpha < 0:
        return None
    return alpha


def _checked(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    if buf.has_error:
        msg = f"Attempted to transform an image. Failed because OpenImageIO reported: {buf.geterror()}"
        raise ValueError(msg)
    return buf


def _is_float(buf: oiio.ImageBuf) -> bool:
    return str(buf.spec().format) in ("half", "float", "double")


def _display_referred(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return scene-linear float pixels with the sRGB transfer function applied to color, clipped to 0-1.

    Raises:
        ValueError: If OpenImageIO cannot read the pixels
    """
    pixels = buf.get_pixels(oiio.FLOAT)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to prepare float pixels for display. Failed because OpenImageIO could not read them: {buf.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    linear = np.clip(pixels, 0.0, 1.0)
    encoded = np.where(linear <= _SRGB_LINEAR_LIMIT, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055).astype(
        np.float32
    )
    source_spec = buf.spec()
    alpha = _alpha_channel(buf)
    if alpha is not None:
        encoded[:, :, alpha] = linear[:, :, alpha]
    out = oiio.ImageBuf(np.ascontiguousarray(encoded))
    out.specmod().channelnames = source_spec.channelnames
    out.specmod().alpha_channel = source_spec.alpha_channel
    return out


def _max_value(dtype: np.dtype) -> float:
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def _oriented(buf: oiio.ImageBuf, orientation: int) -> oiio.ImageBuf:  # noqa: PLR0911
    """Rotate and flip ``buf`` for an EXIF orientation value (2-8).

    Explicit transforms rather than ``ImageBufAlgo.reorient``, which reads the orientation from
    the buffer's own metadata.
    """
    if orientation == 2:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.flop(buf))
    if orientation == 3:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.rotate180(buf))
    if orientation == 4:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.flip(buf))
    if orientation == 5:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.transpose(buf))
    if orientation == 6:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.rotate90(buf))
    if orientation == 7:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.rotate180(_checked(oiio.ImageBufAlgo.transpose(buf))))
    if orientation == 8:  # noqa: PLR2004
        return _checked(oiio.ImageBufAlgo.rotate270(buf))
    return buf.copy()


def _to_rgb(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    order: tuple[int | float, ...] = (0, 1, 2)
    if buf.nchannels in (_GRAYSCALE_CHANNELS, _GRAYSCALE_ALPHA_CHANNELS):
        order = (0, 0, 0)
    return _checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B")))


def _to_rgba(buf: oiio.ImageBuf) -> oiio.ImageBuf:
    channels = buf.nchannels
    if channels == _RGBA_CHANNELS:
        return buf.copy()
    if channels == _GRAYSCALE_CHANNELS:
        order: tuple[int | float, ...] = (0, 0, 0, 1.0)
    elif channels == _GRAYSCALE_ALPHA_CHANNELS:
        order = (0, 0, 0, 1)
    else:
        order = (0, 1, 2, 1.0)
    return _checked(oiio.ImageBufAlgo.channels(buf, order, ("R", "G", "B", "A")))


def _rounded(buf: oiio.ImageBuf, step: RoundCorners) -> oiio.ImageBuf:
    rgba = _to_rgba(buf)
    spec = rgba.spec()
    radius = step.radius_pixels(spec.width, spec.height)
    if radius <= 0:
        return rgba

    pixels = rgba.get_pixels(spec.format)
    if not isinstance(pixels, np.ndarray):
        msg = f"Attempted to round an image's corners. Failed because OpenImageIO could not read its pixels: {rgba.geterror()}"
        raise ValueError(msg)  # noqa: TRY004 - a failed read, not a wrong argument type
    coverage = _rounded_rectangle_coverage(spec.width, spec.height, radius)
    alpha_max = _max_value(pixels.dtype)
    pixels[:, :, _ALPHA] = (coverage * alpha_max).astype(pixels.dtype)
    # An ImageBuf straight over the NumPy array, no file involved
    return oiio.ImageBuf(pixels)


def _flattened(buf: oiio.ImageBuf, step: Flatten) -> oiio.ImageBuf:
    rgba = _to_rgba(buf)
    spec = rgba.spec()
    foreground = oiio.ImageBufAlgo.premult(rgba)
    red, green, blue = step.background
    canvas = oiio.ImageBufAlgo.fill(
        (red / 255.0, green / 255.0, blue / 255.0, 1.0),
        oiio.ROI(0, spec.width, 0, spec.height, 0, 1, 0, _RGBA_CHANNELS),
    )
    composited = _checked(oiio.ImageBufAlgo.over(foreground, canvas))
    rgb = _checked(oiio.ImageBufAlgo.channels(composited, (0, 1, 2), ("R", "G", "B")))
    # over computes in float; return to the source's pixel type
    out = oiio.ImageBuf(oiio.ImageSpec(spec.width, spec.height, _RGB_CHANNELS, spec.format))
    out.copy_pixels(rgb)
    return out


def _rounded_rectangle_coverage(width: int, height: int, radius: int) -> np.ndarray:
    """Return per-pixel coverage (0-1) of a ``width`` x ``height`` rectangle with rounded corners."""
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    # Distance from each pixel to the nearest point of the rectangle inset by radius
    nearest_x = np.clip(xs, radius, width - 1 - radius)
    nearest_y = np.clip(ys, radius, height - 1 - radius)
    distance = np.hypot(xs - nearest_x, ys - nearest_y)
    return np.clip(radius + 0.5 - distance, 0.0, 1.0)
