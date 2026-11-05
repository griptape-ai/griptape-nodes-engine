"""GriptapeImage backed by OpenImageIO."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import ClassVar, NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.containers import add_webp_exif, read_exif, replace_jpeg_exif, replace_png_exif
from griptape_nodes.image.formats import ImageFormat, sniff_image_format
from griptape_nodes.image.griptape_image import GriptapeImage, PixelType
from griptape_nodes.image.metadata import EXIF_ORIENTATION_TAG, Exif, ImageMetadata
from griptape_nodes.image.pipeline import Flatten, Orient, PipelineStep, RoundCorners, Thumbnail, ToRGB, ToRGBA
from griptape_nodes.image.png_text_chunks import insert_text_chunks, read_text_chunks

_GRAYSCALE_CHANNELS = 1
_GRAYSCALE_ALPHA_CHANNELS = 2
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_ALPHA = 3
_UPRIGHT = 1

# Chunks OpenImageIO adds to every PNG besides its empty EXIF: color tags claiming sRGB whatever the
# source was. Dropped so output matches the other backends: untagged, with only the EXIF it's given.
_PNG_DROPPED_CHUNKS = frozenset({b"sRGB", b"gAMA", b"cHRM"})

# Keeps alpha as stored in the file. OpenImageIO otherwise premultiplies on read and divides it
# back out on write, which rounds 8-bit color under partial alpha.
_UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"


class _Decoded(NamedTuple):
    buf: oiio.ImageBuf
    file_format_name: str


class OpenImageIOImage(GriptapeImage):
    """GriptapeImage over an ``OpenImageIO.ImageBuf``.

    Pixels keep the source's bit depth and type (16-bit PNG and TIFF stay 16-bit, EXR stays half
    or float) in R, G, B(, A) order with unassociated alpha. Decoding is deferred until pixels or
    dimensions are first needed, so reading only ``metadata`` of a JPEG, PNG, or WEBP never decodes.

    Pixel buffers stay in memory as ``ImageBuf``s. Encoded bytes don't: reading or writing a file
    in memory needs ``IOProxy``, which the Python bindings don't expose, so in-memory sources and
    encodes go through a temporary file. The bindings also expose EXIF only as parsed attributes,
    so the block is read from and spliced into the container directly, for JPEG, PNG, and WEBP.
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

    # Formats whose EXIF is read straight from the container; the rest report only the
    # orientation OpenImageIO parsed
    _CONTAINER_EXIF_FORMATS: ClassVar[set[ImageFormat]] = {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP}

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

    # Pillow's defaults, so previews come out the same size whichever backend wrote them
    _DEFAULT_QUALITY: ClassVar[dict[ImageFormat, int]] = {ImageFormat.JPEG: 75, ImageFormat.WEBP: 80}

    def __init__(
        self,
        *,
        source: bytes | None,
        source_path: Path | None,
        buf: oiio.ImageBuf | None,
        image_format: ImageFormat | None,
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
    def format(self) -> ImageFormat | None:
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
    def pixel_type(self) -> PixelType:
        type_name = str(self._decoded().spec().format)
        pixel_type = self._PIXEL_TYPES.get(type_name)
        if pixel_type is None:
            msg = f"Attempted to read an image's pixel type. Failed because {type_name} is not a supported pixel type."
            raise ValueError(msg)
        return pixel_type

    @property
    def metadata(self) -> ImageMetadata:
        if self._source is None:
            return ImageMetadata()
        text = {}
        if self._format == ImageFormat.PNG:
            text = read_text_chunks(self._source)
        return ImageMetadata(text=text, exif=self._exif())

    def close(self) -> None:
        self._source = None
        self._source_path = None
        self._buf = None

    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,
        metadata: ImageMetadata | None = None,
    ) -> bytes:
        extension = self._ENCODE_EXTENSIONS.get(image_format)
        if extension is None:
            msg = f"Attempted to save an image as {image_format}. Failed because the OpenImageIO image backend cannot write that format."
            raise ValueError(msg)

        out = self._clean_copy(image_format, quality, optimize=optimize)
        with _temporary_path(extension) as path:
            if not out.write(str(path)):
                msg = f"Attempted to save an image as {image_format}. Failed because OpenImageIO could not encode it: {out.geterror()}"
                raise ValueError(msg)
            encoded = path.read_bytes()

        exif_block = None
        if metadata is not None and metadata.exif is not None:
            exif_block = metadata.exif.to_bytes()
        # OpenImageIO writes an empty EXIF block into PNG and JPEG
        if image_format == ImageFormat.PNG:
            encoded = replace_png_exif(encoded, exif_block, dropped_chunks=_PNG_DROPPED_CHUNKS)
        elif image_format == ImageFormat.JPEG:
            encoded = replace_jpeg_exif(encoded, exif_block)
        elif image_format == ImageFormat.WEBP and exif_block is not None:
            spec = out.spec()
            encoded = add_webp_exif(
                encoded,
                exif_block,
                width=spec.width,
                height=spec.height,
                has_alpha=spec.nchannels == _RGBA_CHANNELS,
            )
        if metadata is not None and metadata.text and image_format == ImageFormat.PNG:
            encoded = insert_text_chunks(encoded, metadata.text)
        return encoded

    def _exif(self) -> Exif | None:
        if self._source is None:
            return None
        if self._format in self._CONTAINER_EXIF_FORMATS:
            block = read_exif(self._source, self._format)
            if block is None:
                return None
            exif = Exif.from_bytes(block)
            if not exif:
                return None
            return exif
        orientation = self._decoded().spec().get_int_attribute("Orientation", _UPRIGHT)
        if orientation == _UPRIGHT:
            return None
        exif = Exif()
        exif[EXIF_ORIENTATION_TAG] = orientation
        return exif

    def _apply_step(self, step: PipelineStep | None) -> OpenImageIOImage:  # noqa: PLR0911
        if step is None:
            return self._derive(self._decoded().copy())
        if isinstance(step, Thumbnail):
            return self._derive(self._thumbnail(step))
        if isinstance(step, Orient):
            return self._derive(_oriented(self._decoded(), step.orientation))
        if isinstance(step, ToRGB):
            return self._derive(_to_rgb(self._decoded()))
        if isinstance(step, ToRGBA):
            return self._derive(_to_rgba(self._decoded()))
        if isinstance(step, RoundCorners):
            return self._derive(_rounded(self._decoded(), step))
        return self._derive(_flattened(self._decoded(), step))

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
        resized = _checked(oiio.ImageBufAlgo.resize(source, "triangle", roi=roi))
        if has_alpha:
            resized = _checked(oiio.ImageBufAlgo.unpremult(resized))
        return resized

    def _clean_copy(self, image_format: ImageFormat, quality: int | None, *, optimize: bool) -> oiio.ImageBuf:
        """Copy the pixels into a buffer with no metadata, so only what ``encode`` is given gets written."""
        buf = self._decoded()
        if buf.nchannels == _GRAYSCALE_ALPHA_CHANNELS and image_format != ImageFormat.PNG:
            # PNG is the only format here that holds gray with alpha
            buf = _to_rgba(buf)
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
        elif image_format == ImageFormat.PNG and optimize:
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
            decoded = _read_file(self._source_path)
        else:
            extension = ""
            if self._format is not None:
                extension = self._ENCODE_EXTENSIONS.get(self._format, "")
            with _temporary_path(extension) as path:
                path.write_bytes(self._source)
                decoded = _read_file(path)
        if self._format is None:
            self._format = ImageFormat.from_name(decoded.file_format_name)
        self._buf = decoded.buf
        return decoded.buf

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
            data = source
            path = None
        else:
            data = source.read_bytes()
            path = source
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
    # every buffer this backend holds is unassociated
    if _alpha_channel(buf) is not None and buf.spec().get_int_attribute(_UNASSOCIATED_ALPHA, 0) != 1:
        buf = _checked(oiio.ImageBufAlgo.unpremult(buf))
    return _Decoded(buf, file_format_name)


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
