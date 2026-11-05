"""GriptapeImage backed by OpenCV."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import cv2
import numpy as np

from griptape_nodes.image.containers import EXIF_HEADER, jpeg_size, read_exif
from griptape_nodes.image.formats import ImageFormat, sniff_image_format
from griptape_nodes.image.griptape_image import GriptapeImage, PixelType
from griptape_nodes.image.metadata import Exif, ImageMetadata
from griptape_nodes.image.pipeline import Flatten, Orient, PipelineStep, RoundCorners, Thumbnail, ToRGB, ToRGBA
from griptape_nodes.image.png_text_chunks import insert_text_chunks, read_text_chunks

if TYPE_CHECKING:
    from pathlib import Path

_GRAYSCALE_CHANNELS = 1
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_ALPHA = 3

# Decode a JPEG at reduced size only if it still leaves at least this many times the thumbnail's
# pixels per side, the margin Pillow's Image.thumbnail keeps (reducing_gap) so the final resize
# still antialiases
_REDUCING_GAP = 2.0


class OpenCVImage(GriptapeImage):
    """GriptapeImage over a NumPy array decoded by OpenCV.

    Pixels keep the source's bit depth (16-bit PNG and TIFF stay 16-bit) and OpenCV's B, G, R(, A)
    channel order. Decoding is deferred until pixels or dimensions are first needed, so reading
    only ``metadata`` of a JPEG, PNG, or WEBP never decodes the image.
    """

    _ENCODE_EXTENSIONS: ClassVar[dict[ImageFormat, str]] = {
        ImageFormat.PNG: ".png",
        ImageFormat.JPEG: ".jpg",
        ImageFormat.WEBP: ".webp",
        ImageFormat.TIFF: ".tiff",
        ImageFormat.BMP: ".bmp",
        ImageFormat.GIF: ".gif",
    }

    # Formats that only hold 8 bits per channel; deeper images are scaled down before encoding
    _EIGHT_BIT_FORMATS: ClassVar[set[ImageFormat]] = {
        ImageFormat.JPEG,
        ImageFormat.WEBP,
        ImageFormat.BMP,
        ImageFormat.GIF,
    }

    # OpenCV writes EXIF only for these
    _EXIF_FORMATS: ClassVar[set[ImageFormat]] = {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP}

    # Formats whose EXIF is read straight from the container; the rest need a full decode
    _CONTAINER_EXIF_FORMATS: ClassVar[set[ImageFormat]] = {ImageFormat.JPEG, ImageFormat.PNG, ImageFormat.WEBP}

    # Pillow's defaults, so previews come out the same size whichever backend wrote them
    _DEFAULT_QUALITY: ClassVar[dict[ImageFormat, int]] = {ImageFormat.JPEG: 75, ImageFormat.WEBP: 80}

    # Reduced-size JPEG decodes, largest reduction first. IGNORE_ORIENTATION leaves EXIF orientation
    # to the pipeline, as IMREAD_UNCHANGED does.
    _REDUCED_DECODES: ClassVar[dict[int, int]] = {
        8: cv2.IMREAD_REDUCED_COLOR_8 | cv2.IMREAD_IGNORE_ORIENTATION,
        4: cv2.IMREAD_REDUCED_COLOR_4 | cv2.IMREAD_IGNORE_ORIENTATION,
        2: cv2.IMREAD_REDUCED_COLOR_2 | cv2.IMREAD_IGNORE_ORIENTATION,
    }

    def __init__(
        self,
        *,
        source: bytes | None,
        pixels: np.ndarray | None,
        image_format: ImageFormat | None,
    ) -> None:
        self._source = source
        self._pixels = pixels
        self._format = image_format

    @property
    def width(self) -> int:
        return self._decoded().shape[1]

    @property
    def height(self) -> int:
        return self._decoded().shape[0]

    @property
    def format(self) -> ImageFormat | None:
        return self._format

    @property
    def channels(self) -> int:
        return _channel_count(self._decoded())

    @property
    def color_space(self) -> str:
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return "Grayscale"
        if channels == _RGBA_CHANNELS:
            return "RGBA"
        return "RGB"

    @property
    def pixel_type(self) -> PixelType:
        return PixelType.from_numpy(self._decoded().dtype)

    @property
    def metadata(self) -> ImageMetadata:
        if self._source is None:
            return ImageMetadata()
        text = {}
        if self._format == ImageFormat.PNG:
            text = read_text_chunks(self._source)
        exif = None
        exif_block = self._exif_block()
        if exif_block is not None:
            exif = Exif.from_bytes(exif_block)
        if not exif:
            exif = None
        return ImageMetadata(text=text, exif=exif)

    def close(self) -> None:
        self._source = None
        self._pixels = None

    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,  # noqa: ARG002 - OpenCV has no equivalent
        metadata: ImageMetadata | None = None,
    ) -> bytes:
        extension = self._ENCODE_EXTENSIONS.get(image_format)
        if extension is None:
            msg = f"Attempted to save an image as {image_format}. Failed because the OpenCV image backend cannot write that format."
            raise ValueError(msg)

        pixels = self._decoded()
        if image_format in self._EIGHT_BIT_FORMATS:
            pixels = _to_eight_bit(pixels)

        params = self._encode_params(image_format, quality)
        exif = None
        if metadata is not None and image_format in self._EXIF_FORMATS:
            exif = metadata.exif
        if exif is not None:
            exif_block = np.frombuffer(exif.to_bytes(), np.uint8)
            ok, buffer = cv2.imencodeWithMetadata(extension, pixels, [cv2.IMAGE_METADATA_EXIF], [exif_block], params)
        else:
            ok, buffer = cv2.imencode(extension, pixels, params)
        if not ok:
            msg = f"Attempted to save an image as {image_format}. Failed because OpenCV could not encode it."
            raise ValueError(msg)

        encoded = buffer.tobytes()
        if metadata is not None and metadata.text and image_format == ImageFormat.PNG:
            encoded = insert_text_chunks(encoded, metadata.text)
        return encoded

    def _exif_block(self) -> bytes | None:
        """Read the source's EXIF from the container where it can be found without decoding, else from a full decode."""
        if self._source is None:
            return None
        if self._format in self._CONTAINER_EXIF_FORMATS:
            return read_exif(self._source, self._format)
        pixels, metadata_types, blocks = cv2.imdecodeWithMetadata(
            np.frombuffer(self._source, np.uint8), cv2.IMREAD_UNCHANGED
        )
        if pixels is not None and pixels.size > 0 and self._pixels is None:
            self._pixels = pixels
        for metadata_type, block in zip(metadata_types, blocks, strict=True):
            if metadata_type == cv2.IMAGE_METADATA_EXIF:
                return bytes(block).removeprefix(EXIF_HEADER)
        return None

    def _apply_step(self, step: PipelineStep | None) -> OpenCVImage:  # noqa: PLR0911
        if step is None:
            return self._derive(self._decoded().copy())
        if isinstance(step, Thumbnail):
            return self._derive(self._thumbnail(step))
        if isinstance(step, Orient):
            return self._derive(_apply_orientation(self._decoded(), step.orientation))
        if isinstance(step, ToRGB):
            return self._derive(_to_rgb(self._decoded()))
        if isinstance(step, ToRGBA):
            return self._derive(_to_rgba(self._decoded()))
        if isinstance(step, RoundCorners):
            return self._derive(_rounded(self._decoded(), step))
        return self._derive(_flattened(self._decoded(), step))

    def _thumbnail(self, step: Thumbnail) -> np.ndarray:
        reduced = self._reduced_jpeg_decode(step)
        if reduced is not None:
            return reduced
        pixels = self._decoded()
        height, width = pixels.shape[:2]
        scale = min(step.max_width / width, step.max_height / height)
        if scale >= 1:
            return pixels.copy()
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return cv2.resize(pixels, size, interpolation=cv2.INTER_AREA)

    def _reduced_jpeg_decode(self, step: Thumbnail) -> np.ndarray | None:
        """Shrink a not-yet-decoded JPEG source by decoding it at 1/2, 1/4, or 1/8 size first, or return None.

        A grayscale JPEG comes back as three-channel gray, since the reduced decodes are color only.
        """
        if self._pixels is not None or self._source is None or self._format != ImageFormat.JPEG:
            return None
        size = jpeg_size(self._source)
        if size is None:
            return None
        scale = min(step.max_width / size.width, step.max_height / size.height)
        factor = next((factor for factor in self._REDUCED_DECODES if scale * factor * _REDUCING_GAP <= 1), None)
        if factor is None:
            return None
        reduced = cv2.imdecode(np.frombuffer(self._source, np.uint8), self._REDUCED_DECODES[factor])
        if reduced is None or reduced.size == 0:
            return None
        target = (max(1, round(size.width * scale)), max(1, round(size.height * scale)))
        return cv2.resize(reduced, target, interpolation=cv2.INTER_AREA)

    def _decoded(self) -> np.ndarray:
        """Return the pixels, decoding the source on first use.

        Raises:
            ValueError: If the image was closed or OpenCV cannot decode the source
        """
        if self._pixels is not None:
            return self._pixels
        if self._source is None:
            msg = "Attempted to read an image's pixels. Failed because the image was closed."
            raise ValueError(msg)

        # IMREAD_UNCHANGED keeps bit depth and alpha, and leaves EXIF orientation to the pipeline
        pixels = cv2.imdecode(np.frombuffer(self._source, np.uint8), cv2.IMREAD_UNCHANGED)
        if pixels is None or pixels.size == 0:
            msg = "Attempted to decode an image. Failed because OpenCV does not recognize the data as an image."
            raise ValueError(msg)
        self._pixels = pixels
        return pixels

    def _derive(self, pixels: np.ndarray) -> OpenCVImage:
        return type(self)(source=None, pixels=pixels, image_format=self._format)

    def _encode_params(self, image_format: ImageFormat, quality: int | None) -> list[int]:
        if quality is None:
            quality = self._DEFAULT_QUALITY.get(image_format)
        if quality is None:
            return []
        if image_format == ImageFormat.JPEG:
            return [cv2.IMWRITE_JPEG_QUALITY, quality]
        if image_format == ImageFormat.WEBP:
            return [cv2.IMWRITE_WEBP_QUALITY, quality]
        return []

    @classmethod
    def open(cls, source: bytes | Path) -> OpenCVImage:
        """Open an image without decoding it. Pixels decode on first use.

        Reads files through Python rather than ``cv2.imread``, which cannot open non-ASCII paths on Windows.

        Raises:
            OSError: If the file cannot be read
            ValueError: If the data is not in a format this backend reads
        """
        if isinstance(source, bytes):
            data = source
        else:
            data = source.read_bytes()
        image_format = sniff_image_format(data)
        if image_format is None:
            msg = (
                "Attempted to open an image. Failed because the data is not a PNG, JPEG, GIF, WEBP, BMP, or TIFF image."
            )
            raise ValueError(msg)
        return cls(source=data, pixels=None, image_format=image_format)


def _channel_count(pixels: np.ndarray) -> int:
    if pixels.ndim == 2:  # noqa: PLR2004
        return _GRAYSCALE_CHANNELS
    return pixels.shape[2]


def _max_value(dtype: np.dtype) -> float:
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def _to_eight_bit(pixels: np.ndarray) -> np.ndarray:
    if pixels.dtype == np.uint8:
        return pixels
    scaled = pixels.astype(np.float64) / _max_value(pixels.dtype) * 255.0
    return np.clip(np.round(scaled), 0, 255).astype(np.uint8)


def _to_rgb(pixels: np.ndarray) -> np.ndarray:
    channels = _channel_count(pixels)
    if channels == _GRAYSCALE_CHANNELS:
        return cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
    if channels == _RGBA_CHANNELS:
        return cv2.cvtColor(pixels, cv2.COLOR_BGRA2BGR)
    return pixels.copy()


def _to_rgba(pixels: np.ndarray) -> np.ndarray:
    channels = _channel_count(pixels)
    if channels == _GRAYSCALE_CHANNELS:
        return cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGRA)
    if channels == _RGB_CHANNELS:
        return cv2.cvtColor(pixels, cv2.COLOR_BGR2BGRA)
    return pixels.copy()


def _rounded(pixels: np.ndarray, step: RoundCorners) -> np.ndarray:
    rgba = _to_rgba(pixels)
    height, width = rgba.shape[:2]
    radius = step.radius_pixels(width, height)
    if radius <= 0:
        return rgba

    mask = np.zeros((height, width), dtype=np.uint8)
    right = width - 1
    bottom = height - 1
    cv2.rectangle(mask, (radius, 0), (right - radius, bottom), 255, thickness=cv2.FILLED)
    cv2.rectangle(mask, (0, radius), (right, bottom - radius), 255, thickness=cv2.FILLED)
    for center in (
        (radius, radius),
        (right - radius, radius),
        (radius, bottom - radius),
        (right - radius, bottom - radius),
    ):
        cv2.circle(mask, center, radius, 255, thickness=cv2.FILLED, lineType=cv2.LINE_AA)

    alpha_max = _max_value(rgba.dtype)
    rgba[:, :, _ALPHA] = (mask.astype(np.float64) / 255.0 * alpha_max).astype(rgba.dtype)
    return rgba


def _flattened(pixels: np.ndarray, step: Flatten) -> np.ndarray:
    rgba = _to_rgba(pixels)
    value_max = _max_value(rgba.dtype)
    alpha = rgba[:, :, _ALPHA : _ALPHA + 1].astype(np.float64) / value_max
    # background is RGB at 8 bits; pixels are BGR at the image's depth
    red, green, blue = step.background
    canvas = np.array([blue, green, red], dtype=np.float64) / 255.0 * value_max
    composited = rgba[:, :, :_ALPHA].astype(np.float64) * alpha + canvas * (1.0 - alpha)
    if np.issubdtype(rgba.dtype, np.integer):
        composited = np.round(composited)
    return composited.astype(rgba.dtype)


def _apply_orientation(pixels: np.ndarray, orientation: int) -> np.ndarray:  # noqa: PLR0911
    """Rotate and flip ``pixels`` for an EXIF orientation value (2-8)."""
    if orientation == 2:  # noqa: PLR2004
        return cv2.flip(pixels, 1)
    if orientation == 3:  # noqa: PLR2004
        return cv2.rotate(pixels, cv2.ROTATE_180)
    if orientation == 4:  # noqa: PLR2004
        return cv2.flip(pixels, 0)
    if orientation == 5:  # noqa: PLR2004
        return cv2.transpose(pixels)
    if orientation == 6:  # noqa: PLR2004
        return cv2.rotate(pixels, cv2.ROTATE_90_CLOCKWISE)
    if orientation == 7:  # noqa: PLR2004
        return cv2.flip(cv2.transpose(pixels), -1)
    if orientation == 8:  # noqa: PLR2004
        return cv2.rotate(pixels, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return pixels.copy()
