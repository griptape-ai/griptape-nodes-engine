"""The image type the engine decodes, transforms, and encodes images through."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NamedTuple, Self

from griptape_nodes.image.cache import image_source_access
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
    TRANSPOSING_ORIENTATIONS,
    Exif,
    tiff_metadata,
)
from griptape_nodes.image.formats import (
    SNIFF_MIN_HEADER_BYTES,
    ImageFormat,
    sniff_image_format,
)
from griptape_nodes.image.image_io import (
    Header,
    read_cached_file,
    read_ephemeral_file,
    read_header_file,
    spec_icc_profile,
    temporary_path,
)
from griptape_nodes.image.metadata import ImageMetadata
from griptape_nodes.image.operations import (
    GRAYSCALE_ALPHA_CHANNELS,
    GRAYSCALE_CHANNELS,
    RGBA_CHANNELS,
    cover,
    flattened,
    oriented,
    rounded,
    thumbnail,
    to_rgb,
    to_rgba,
)
from griptape_nodes.image.path_metadata import read_path_metadata
from griptape_nodes.image.pipeline import Cover, Flatten, Orient, PipelineStep, RoundCorners, Thumbnail, ToRGB, ToRGBA
from griptape_nodes.image.pixel_type import PixelType
from griptape_nodes.image.png_text_chunks import read_text_chunks

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    import OpenImageIO as oiio  # noqa: N813

logger = logging.getLogger("griptape_nodes")

_UPRIGHT = 1


class ImageDimensions(NamedTuple):
    """Width and height in pixels."""

    width: int
    height: int


class GriptapeImage:
    """An image the engine decodes, transforms, and encodes through OpenImageIO.

    Instances are immutable. Open one with ``open`` and transform it with
    ``GriptapeImagePipeline``, which returns a new image carrying the source's metadata, updated to
    describe the new pixels. Use as a context manager, or call ``close``, to release the source
    early.

    When opened from a ``Path``, file bytes are read lazily: header inspection uses OpenImageIO's
    ``ImageInput`` directly on the path without loading the whole file into Python ``bytes``, and
    pixel decoding flows through OpenImageIO's shared ``ImageCache``. When opened from ``bytes``,
    ephemeral temporary files bypass ``ImageCache`` so temporary paths never consume cache memory
    or hold open file handles.
    """

    _SPLICEABLE_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
        {ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.WEBP}
    )

    _CONTAINER_METADATA_FORMATS: ClassVar[frozenset[ImageFormat]] = frozenset(
        {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP, ImageFormat.TIFF}
    )

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
        self._source = source
        self._source_path = source_path
        self._buf = buf
        self._format = image_format
        self._metadata = metadata
        self._extra_kinds = extra_metadata_kinds
        self._display_encoded = display_encoded
        self._header: Header | None = None
        self._closed = False

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
        """Stored width in pixels."""
        return self._spec().width

    @property
    def height(self) -> int:
        """Stored height in pixels."""
        return self._spec().height

    @property
    def oriented_size(self) -> ImageDimensions:
        """Displayed (width, height) after applying EXIF/header Orientation, without decoding pixels."""
        spec = self._spec()
        width = spec.width
        height = spec.height
        orientation = self._header_orientation(spec)
        if orientation in TRANSPOSING_ORIENTATIONS:
            return ImageDimensions(width=height, height=width)
        return ImageDimensions(width=width, height=height)

    @property
    def format(self) -> ImageFormat | None:
        """Source container format, or None if not recognized."""
        return self._format

    @property
    def channels(self) -> int:
        """Number of channels per pixel, alpha included."""
        return self._spec().nchannels

    @property
    def color_space(self) -> str:
        """Human-readable color model: ``"RGB"``, ``"RGBA"``, ``"Grayscale"``, or ``"Grayscale+Alpha"``."""
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
        """The image's text, EXIF, and color profile. Empty once the image is closed."""
        if self._closed:
            return ImageMetadata()
        if self._metadata is None:
            if self._source_path is not None and self._format in self._CONTAINER_METADATA_FORMATS:
                assert self._format is not None  # noqa: S101
                with image_source_access(self._source_path):
                    self._metadata = read_path_metadata(self._source_path, self._format)
            else:
                source = self._source
                if source is None and self._source_path is None:
                    return ImageMetadata()
                self._metadata = self._read_metadata(source)
        exif = None
        if self._metadata.exif is not None:
            exif = self._metadata.exif.copy()
        return replace(self._metadata, text=dict(self._metadata.text), exif=exif)

    @property
    def orientation(self) -> int:
        """Display orientation read from attached metadata or the image header."""
        return self._header_orientation(self._spec())

    def with_metadata(self, metadata: ImageMetadata) -> GriptapeImage:
        """Return an independent image wrapper carrying the supplied metadata."""
        exif = None
        if metadata.exif is not None:
            exif = metadata.exif.copy()
        return type(self)(
            source=self._source,
            source_path=self._source_path,
            buf=self._buf,
            image_format=self._format,
            metadata=replace(metadata, text=dict(metadata.text), exif=exif),
            extra_metadata_kinds=self._extra_kinds,
            display_encoded=self._display_encoded,
        )

    def close(self) -> None:
        """Release the source and decoded pixels."""
        self._closed = True
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
        """Encode the image to ``image_format`` bytes."""
        if image_format not in ENCODE_EXTENSIONS:
            msg = f"Attempted to save an image as {image_format}. Failed because that format cannot be written."
            raise ValueError(msg)

        source = self._source_bytes_for_splice(image_format)
        own_metadata = metadata is None
        if metadata is None:
            requested = self.metadata
        else:
            requested = metadata
        dropped = unwritable_kinds(image_format, requested)
        writes_some = own_metadata or not requested.is_empty()
        if (
            source is not None
            and writes_some
            and self._can_splice(source, image_format, quality, requested, optimize=optimize)
        ):
            encoded = source
            if not own_metadata:
                encoded = spliced(source, image_format, requested, dropped)
        else:
            if self._source_path is not None:
                with image_source_access(self._source_path):
                    encoded = reencoded(
                        self._decoded(),
                        image_format,
                        quality,
                        requested,
                        dropped,
                        optimize=optimize,
                        holds_display_values=self._holds_display_values(),
                    )
            else:
                encoded = reencoded(
                    self._decoded(),
                    image_format,
                    quality,
                    requested,
                    dropped,
                    optimize=optimize,
                    holds_display_values=self._holds_display_values(),
                )
            if own_metadata or requested.exif or requested.text:
                dropped.extend(self._extra_metadata_kinds())
        if dropped:
            logger.warning("Saved an image as %s without its %s.", image_format, spoken_list(dropped))
        return encoded

    def _header_orientation(self, spec: oiio.ImageSpec) -> int:
        if self._metadata is not None and self._metadata.exif is not None:
            return self._metadata.exif.orientation
        if self._source is not None and self._format in self._CONTAINER_METADATA_FORMATS:
            exif = self.metadata.exif
            if exif is not None:
                return exif.orientation
        return spec.get_int_attribute("Orientation", _UPRIGHT)

    def _source_bytes_for_splice(self, target_format: ImageFormat) -> bytes | None:
        if self._source is not None:
            return self._source
        if (
            self._source_path is not None
            and target_format == self._format
            and target_format in self._SPLICEABLE_FORMATS
        ):
            with image_source_access(self._source_path):
                self._source = self._source_path.read_bytes()
            return self._source
        return None

    def _can_splice(
        self,
        source: bytes,
        image_format: ImageFormat,
        quality: int | None,
        requested: ImageMetadata,
        *,
        optimize: bool,
    ) -> bool:
        if image_format != self._format or image_format not in self._SPLICEABLE_FORMATS:
            return False
        if quality is not None or optimize:
            return False
        if requested.icc_profile != self.metadata.icc_profile:
            return False
        return is_complete(source, image_format)

    def _holds_display_values(self) -> bool:
        if self._display_encoded is None:
            return self.metadata.icc_profile is not None
        return self._display_encoded

    def _extra_metadata_kinds(self) -> list[str]:
        if self._extra_kinds is None:
            source = self._source
            if source is None:
                return []
            self._extra_kinds = read_extra_metadata_kinds(source, self._format)
        return list(self._extra_kinds)

    def _read_metadata(self, source: bytes | None) -> ImageMetadata:
        image_format = self._format
        text: dict[str, str] = {}
        if source is not None and image_format == ImageFormat.PNG:
            text = read_text_chunks(source)
        if source is not None and image_format == ImageFormat.TIFF:
            parsed = tiff_metadata(source)
            exif = None
            if parsed.exif:
                exif = parsed.exif
            return ImageMetadata(text=text, exif=exif, icc_profile=parsed.icc_profile)
        if source is not None and image_format is not None and image_format in self._CONTAINER_METADATA_FORMATS:
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

    def run_pipeline(self, steps: Sequence[PipelineStep]) -> GriptapeImage:
        """Apply validated image steps while coordinating cached source consumption."""
        if self._source_path is not None:
            with image_source_access(self._source_path):
                return self._run_pipeline(steps)
        return self._run_pipeline(steps)

    def _run_pipeline(self, steps: Sequence[PipelineStep]) -> GriptapeImage:
        """Apply image steps and release intermediate images."""
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
        if step is None:
            return self._derive(self._decoded().copy(), step)
        if isinstance(step, Flatten):
            return self._derive(flattened(self._decoded(), step), step)
        if isinstance(step, Cover):
            return self._derive(cover(self._decoded(), step), step)
        if isinstance(step, Thumbnail):
            return self._derive(thumbnail(self._decoded(), step), step)
        if isinstance(step, Orient):
            return self._derive(oriented(self._decoded(), step.orientation), step)
        if isinstance(step, ToRGB):
            return self._derive(to_rgb(self._decoded()), step)
        if isinstance(step, ToRGBA):
            return self._derive(to_rgba(self._decoded()), step)
        if isinstance(step, RoundCorners):
            return self._derive(rounded(self._decoded(), step), step)
        msg = f"Attempted to transform an image. Failed because the step {type(step).__name__} is unsupported."
        raise TypeError(msg)

    def _derive(self, buf: oiio.ImageBuf, step: PipelineStep | None) -> GriptapeImage:
        metadata = self.metadata
        for attribute in [attribute.name for attribute in buf.spec().extra_attribs]:
            buf.specmod().erase_attribute(attribute)
        icc_profile = metadata.icc_profile
        if isinstance(step, Flatten):
            icc_profile = None
        if icc_profile is not None and not icc_profile_fits(icc_profile, buf.nchannels):
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
        if self._buf is not None:
            return self._buf.spec()
        return self._read_header().spec

    def _read_header(self) -> Header:
        if self._header is not None:
            return self._header
        if self._closed or (self._source is None and self._source_path is None):
            msg = "Attempted to read an image's header. Failed because the image was closed."
            raise ValueError(msg)
        if self._source_path is not None:
            with image_source_access(self._source_path):
                header = read_header_file(self._source_path)
        else:
            assert self._source is not None  # noqa: S101
            with temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                header = read_header_file(path)
        self._header = header
        return header

    def _source_extension(self) -> str:
        if self._format is None:
            return ""
        return f".{self._format.extension}"

    def _decoded(self) -> oiio.ImageBuf:
        if self._buf is not None:
            return self._buf
        if self._closed or (self._source is None and self._source_path is None):
            msg = "Attempted to read an image's pixels. Failed because the image was closed."
            raise ValueError(msg)

        if self._source_path is not None:
            decoded = read_cached_file(self._source_path)
        else:
            assert self._source is not None  # noqa: S101
            with temporary_path(self._source_extension()) as path:
                path.write_bytes(self._source)
                decoded = read_ephemeral_file(path)
        if self._format is None:
            self._format = ImageFormat.from_name(decoded.file_format_name)
        self._buf = decoded.buf
        return decoded.buf

    @classmethod
    def open(cls, source: bytes | Path | str) -> GriptapeImage:
        """Open an image from encoded bytes or a file path without decoding pixels.

        Raises:
            OSError: If the file cannot be read
            ValueError: If the data is not an image OpenImageIO can read
        """
        if isinstance(source, bytes):
            image = cls(source=source, source_path=None, buf=None, image_format=sniff_image_format(source))
        else:
            path = Path(source) if isinstance(source, str) else source
            with path.open("rb") as handle:
                head = handle.read(SNIFF_MIN_HEADER_BYTES)
            image = cls(source=None, source_path=path, buf=None, image_format=sniff_image_format(head))
        header = image._read_header()
        if image.format is None:
            image._format = ImageFormat.from_name(header.file_format_name)
        return image


def _container_exif(source: bytes, image_format: ImageFormat) -> Exif | None:
    block = read_exif(source, image_format)
    if block is None:
        return None
    exif = Exif.from_bytes(block)
    if not exif:
        return None
    return exif


def _describe_pixels(exif: Exif, spec: oiio.ImageSpec, *, oriented: bool) -> None:
    if oriented:
        if EXIF_ORIENTATION_TAG in exif and exif.orientation != _UPRIGHT:
            exif[EXIF_ORIENTATION_TAG] = _UPRIGHT
        exif.remove_thumbnail()
    if EXIF_PIXEL_X_DIMENSION_TAG in exif:
        exif[EXIF_PIXEL_X_DIMENSION_TAG] = spec.width
    if EXIF_PIXEL_Y_DIMENSION_TAG in exif:
        exif[EXIF_PIXEL_Y_DIMENSION_TAG] = spec.height
