"""The image type the engine decodes, transforms, and encodes images through."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Self, assert_never

import numpy as np

from griptape_nodes.image.containers import (
    is_complete,
    read_exif,
    read_extra_metadata_kinds,
    read_icc_profile,
)
from griptape_nodes.image.encoding import (
    ENCODE_EXTENSIONS,
    icc_profile_fits,
    reencoded,
    spliced,
    spoken_list,
    unwritable_kinds,
)
from griptape_nodes.image.exif import (
    EXIF_ORIENTATION_TAG,
    EXIF_PIXEL_X_DIMENSION_TAG,
    EXIF_PIXEL_Y_DIMENSION_TAG,
    Exif,
    tiff_metadata,
)
from griptape_nodes.image.formats import ImageFormat, sniff_image_format
from griptape_nodes.image.image_io import (
    Header,
    read_file,
    read_header_file,
    spec_icc_profile,
    temporary_path,
)
from griptape_nodes.image.metadata import ImageMetadata
from griptape_nodes.image.operations import (
    GRAYSCALE_ALPHA_CHANNELS,
    GRAYSCALE_CHANNELS,
    RGBA_CHANNELS,
    applied,
    blurred,
    buffer_from_pixels,
    composited,
    cropped,
    dilated,
    display_referred,
    edges,
    enhanced,
    eroded,
    flattened,
    flipped_horizontal,
    flipped_vertical,
    inverted,
    is_float,
    native_pixels,
    new_image,
    oriented,
    padded,
    resized,
    rotated,
    rounded,
    scene_linear,
    thumbnail,
    to_gray,
    to_rgb,
    to_rgba,
    to_uint8,
    with_alpha,
    with_line,
    with_rectangle,
    with_text,
)
from griptape_nodes.image.pipeline import (
    Apply,
    Brightness,
    Color,
    Composite,
    Contrast,
    Crop,
    Dilate,
    DrawLine,
    DrawRectangle,
    DrawText,
    Erode,
    FindEdges,
    Flatten,
    FlipHorizontal,
    FlipVertical,
    GaussianBlur,
    Invert,
    Orient,
    Pad,
    PipelineStep,
    Resize,
    Rotate,
    RoundCorners,
    Saturation,
    SetAlpha,
    Thumbnail,
    ToGray,
    ToRGB,
    ToRGBA,
    ToUint8,
)
from griptape_nodes.image.pixel_type import PixelType
from griptape_nodes.image.png_text_chunks import read_text_chunks

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from types import TracebackType

    import OpenImageIO as oiio  # noqa: N813

logger = logging.getLogger("griptape_nodes")

_UPRIGHT = 1


class GriptapeImage:
    """An image the engine decodes, transforms, and encodes.

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
    """

    # Built on OpenImageIO, which callers don't see. Pixel buffers stay in memory. Encoded bytes
    # don't: reading or writing a file in memory needs ``IOProxy``, which the OpenImageIO Python
    # bindings don't expose, so in-memory sources and encodes go through a temporary file. The
    # bindings also expose EXIF only as parsed attributes, so the block is read from and spliced
    # into the container directly. OpenImageIO 3.2 has no reduced-size JPEG decode, so a
    # thumbnail of a large JPEG decodes the full image first.

    # Formats whose source bytes can take new metadata without touching the pixel data
    _SPLICEABLE_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset({ImageFormat.PNG, ImageFormat.JPEG})

    # Formats whose EXIF and color profile are read straight from the container; the rest report
    # only the orientation and profile OpenImageIO parsed
    _CONTAINER_METADATA_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
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
        self._header: Header | None = None

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
        if channels == GRAYSCALE_CHANNELS:
            return "Grayscale"
        if channels == GRAYSCALE_ALPHA_CHANNELS:
            return "Grayscale+Alpha"
        if channels == RGBA_CHANNELS:
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

    def to_array(self) -> np.ndarray:
        """Return a height x width x channels NumPy copy of the pixels, in their own type, alpha unassociated and last.

        Raises:
            ValueError: If the image was closed or can't be decoded
        """
        return native_pixels(self._decoded())

    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,
        lossless: bool = False,
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
            lossless: Store WEBP without loss, ignoring ``quality``. The other formats ``encode``
                writes are lossless already, except JPEG, which can't be.
            metadata: Metadata to write. None writes the image's own; ``ImageMetadata()`` writes none.
                PNG holds all of it; JPEG, WEBP, and TIFF hold EXIF and the color profile; BMP,
                GIF, TGA, and EXR hold none.

        Returns:
            The encoded file bytes

        Raises:
            ValueError: If the format can't be written or OpenImageIO fails to encode the pixels
        """
        if image_format not in ENCODE_EXTENSIONS:
            msg = f"Attempted to save an image as {image_format}. Failed because that format can't be written."
            raise ValueError(msg)
        if lossless and image_format == ImageFormat.JPEG:
            msg = "Attempted to save an image as lossless JPEG. Failed because JPEG is always lossy."
            raise ValueError(msg)

        source = self._source
        own_metadata = metadata is None
        if metadata is None:
            requested = self.metadata
        else:
            requested = metadata
        dropped = unwritable_kinds(image_format, requested)
        # ``ImageMetadata()`` asks for no metadata at all, XMP and IPTC included, which only a re-encode drops
        writes_some = own_metadata or not requested.is_empty()
        if (
            source is not None
            and writes_some
            and not lossless
            and self._can_splice(source, image_format, quality, requested, optimize=optimize)
        ):
            encoded = source
            if not own_metadata:
                encoded = spliced(source, image_format, requested, dropped)
        else:
            encoded = reencoded(
                self._decoded(),
                image_format,
                quality,
                requested,
                dropped,
                optimize=optimize,
                holds_display_values=self._holds_display_values(),
                lossless=lossless,
            )
            if own_metadata or requested.exif or requested.text:
                dropped.extend(self._extra_metadata_kinds())
        if dropped:
            logger.warning("Saved an image as %s without its %s.", image_format, spoken_list(dropped))
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
        return ImageMetadata(text=text, exif=exif, icc_profile=spec_icc_profile(spec))

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

    def _apply_step(self, step: PipelineStep | None) -> GriptapeImage:
        """Return a new image with ``step`` applied, or an unchanged copy for None."""
        return self._derive(self._step_pixels(step), step)

    def _step_pixels(self, step: PipelineStep | None) -> oiio.ImageBuf:  # noqa: C901, PLR0911, PLR0912
        """Return the pixels ``step`` makes from this image's."""
        buf = self._decoded()
        match step:
            case None:
                return buf.copy()
            case Thumbnail():
                return thumbnail(buf, step)
            case Orient():
                return oriented(buf, step.orientation)
            case ToRGB():
                return to_rgb(buf)
            case ToRGBA():
                return to_rgba(buf)
            case RoundCorners():
                return rounded(buf, step)
            case Flatten():
                return flattened(buf, step)
            case Resize():
                return resized(buf, step.width, step.height, step.resize_filter)
            case Crop():
                return cropped(buf, step)
            case Rotate():
                return rotated(buf, step)
            case FlipHorizontal():
                return flipped_horizontal(buf)
            case FlipVertical():
                return flipped_vertical(buf)
            case Pad():
                return padded(buf, step)
            case ToGray():
                return to_gray(buf)
            case ToUint8():
                return to_uint8(buf, holds_display_values=self._holds_display_values())
            case Brightness() | Contrast() | Saturation():
                return enhanced(buf, step)
            case Invert():
                return inverted(buf)
            case GaussianBlur():
                return blurred(buf, step.radius)
            case Dilate():
                return dilated(buf, step.size)
            case Erode():
                return eroded(buf, step.size)
            case FindEdges():
                return edges(buf)
            case Composite():
                return composited(buf, self._encoded_like_self(step.overlay), step)
            case SetAlpha():
                alpha = step.alpha
                if isinstance(alpha, GriptapeImage):
                    return with_alpha(buf, alpha._decoded())
                return with_alpha(buf, alpha)
            case DrawText():
                return with_text(buf, step)
            case DrawRectangle():
                return with_rectangle(buf, step)
            case DrawLine():
                return with_line(buf, step)
            case Apply():
                return applied(buf, step.function)
            case _:
                assert_never(step)

    def _encodes_display(self) -> bool:
        """Whether the pixels hold display values: integer pixels always do, float ones may be scene-linear."""
        return not is_float(self._decoded()) or self._holds_display_values()

    def _encoded_like_self(self, other: GriptapeImage) -> oiio.ImageBuf:
        """Return ``other``'s pixels with the same encoding as this image's, so the two can be mixed."""
        buf = other._decoded()
        if self._encodes_display() == other._encodes_display():
            return buf
        if self._encodes_display():
            return display_referred(buf)
        return scene_linear(buf)

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
        if icc_profile is not None and not icc_profile_fits(icc_profile, buf.nchannels):
            icc_profile = None
        exif = metadata.exif
        if exif is not None:
            _describe_pixels(exif, buf.spec(), oriented=isinstance(step, Orient))
        # Float pixels a step computed from integer ones hold the same display values, scaled to 0-1
        display_encoded = self._holds_display_values() or not is_float(self._decoded())
        return type(self)(
            source=None,
            source_path=None,
            buf=buf,
            image_format=self._format,
            metadata=replace(metadata, exif=exif, icc_profile=icc_profile),
            extra_metadata_kinds=self._extra_metadata_kinds(),
            display_encoded=display_encoded,
        )

    def _spec(self) -> oiio.ImageSpec:
        """Return the decoded pixels' spec, or the source header's if the pixels aren't decoded yet.

        Raises:
            ValueError: If the image was closed or OpenImageIO cannot read the source's header
        """
        if self._buf is not None:
            return self._buf.spec()
        return self._read_header().spec

    def _read_header(self) -> Header:
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
            header = read_header_file(self._source_path)
        else:
            with temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                header = read_header_file(path)
        self._header = header
        return header

    def _source_extension(self) -> str:
        if self._format is None:
            return ""
        return ENCODE_EXTENSIONS.get(self._format, "")

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
            decoded = read_file(self._source_path)
        else:
            with temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                decoded = read_file(path)
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

    @classmethod
    def from_array(cls, pixels: np.ndarray, metadata: ImageMetadata | None = None) -> GriptapeImage:
        """Make an image from a height x width (x channels) NumPy array.

        One channel is gray, two gray and alpha, three RGB, four RGBA, with unassociated alpha.
        Integer types use their full range; float pixels are 0-1 and, as with EXR, taken as
        scene-linear unless ``metadata`` carries a color profile. The array is copied. The image
        has no format until it's encoded.

        Raises:
            ValueError: If the array's shape or type isn't one an image can have
        """
        PixelType.from_numpy(pixels.dtype)
        buf = buffer_from_pixels(np.array(pixels))
        if metadata is None:
            metadata = ImageMetadata()
        return cls(
            source=None, source_path=None, buf=buf, image_format=None, metadata=metadata, extra_metadata_kinds=[]
        )

    @classmethod
    def new(cls, width: int, height: int, color: Color = (0, 0, 0)) -> GriptapeImage:
        """Make an 8-bit image of one color: gray, gray and alpha, RGB, or RGBA, as many channels as ``color`` has.

        Raises:
            ValueError: If the size isn't positive or the color doesn't have 1-4 values
        """
        return cls(
            source=None,
            source_path=None,
            buf=new_image(width, height, color),
            image_format=None,
            metadata=ImageMetadata(),
            extra_metadata_kinds=[],
        )


def _container_exif(source: bytes, image_format: ImageFormat) -> Exif | None:
    block = read_exif(source, image_format)
    if block is None:
        return None
    exif = Exif.from_bytes(block)
    if not exif:
        return None
    return exif


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
