"""GriptapeImage backed by OpenCV."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, ClassVar

import cv2
import numpy as np

from griptape_nodes.image.formats import sniff_image_format
from griptape_nodes.image.griptape_image import GriptapeImage, normalize_format_name
from griptape_nodes.image.png_text_chunks import insert_text_chunks, read_text_chunks

if TYPE_CHECKING:
    from pathlib import Path

_EXIF_HEADER = b"Exif\x00\x00"
_EXIF_ORIENTATION_TAG = 0x0112
_IFD_ENTRY_BYTES = 12

_GRAYSCALE_CHANNELS = 1
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_ALPHA = 3


class OpenCVImage(GriptapeImage):
    """GriptapeImage over a NumPy array decoded by OpenCV.

    Pixels keep the source's bit depth (16-bit PNG and TIFF stay 16-bit) and OpenCV's B, G, R(, A)
    channel order. Decoding is deferred until pixels or dimensions are first needed, so reading
    only ``text_metadata`` never decodes the image.
    """

    _ENCODE_EXTENSIONS: ClassVar[dict[str, str]] = {
        "PNG": ".png",
        "JPEG": ".jpg",
        "WEBP": ".webp",
        "TIFF": ".tiff",
        "BMP": ".bmp",
        "GIF": ".gif",
    }

    # Formats that only hold 8 bits per channel; deeper images are scaled down before encoding
    _EIGHT_BIT_FORMATS: ClassVar[set[str]] = {"JPEG", "WEBP", "BMP", "GIF"}

    # OpenCV writes EXIF only for these
    _EXIF_FORMATS: ClassVar[set[str]] = {"JPEG", "PNG", "WEBP"}

    # Pillow's defaults, so previews come out the same size whichever backend wrote them
    _DEFAULT_QUALITY: ClassVar[dict[str, int]] = {"JPEG": 75, "WEBP": 80}

    def __init__(
        self,
        *,
        source: bytes | None,
        pixels: np.ndarray | None,
        image_format: str | None,
    ) -> None:
        self._source = source
        self._pixels = pixels
        self._format = image_format
        self._exif: bytes | None = None

    @property
    def width(self) -> int:
        return self._decoded().shape[1]

    @property
    def height(self) -> int:
        return self._decoded().shape[0]

    @property
    def format(self) -> str | None:
        return self._format

    @property
    def channels(self) -> int:
        pixels = self._decoded()
        if pixels.ndim == 2:  # noqa: PLR2004
            return _GRAYSCALE_CHANNELS
        return pixels.shape[2]

    @property
    def color_space(self) -> str:
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return "Grayscale"
        if channels == _RGBA_CHANNELS:
            return "RGBA"
        return "RGB"

    @property
    def bit_depth(self) -> int:
        return self._decoded().dtype.itemsize * 8

    @property
    def text_metadata(self) -> dict[str, str]:
        if self._source is None or self._format != "PNG":
            return {}
        return read_text_chunks(self._source)

    @property
    def exif_bytes(self) -> bytes | None:
        if self._source is None:
            return None
        self._decoded()
        return self._exif

    def close(self) -> None:
        self._source = None
        self._pixels = None

    def exif_transposed(self) -> OpenCVImage:
        pixels = self._decoded()
        orientation = _exif_orientation(self.exif_bytes)
        return self._derive(_apply_orientation(pixels, orientation))

    def thumbnail(self, max_width: int, max_height: int) -> OpenCVImage:
        pixels = self._decoded()
        height, width = pixels.shape[:2]
        scale = min(max_width / width, max_height / height)
        if scale >= 1:
            return self._derive(pixels.copy())
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return self._derive(cv2.resize(pixels, size, interpolation=cv2.INTER_AREA))

    def to_rgb(self) -> OpenCVImage:
        pixels = self._decoded()
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return self._derive(cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR))
        if channels == _RGBA_CHANNELS:
            return self._derive(cv2.cvtColor(pixels, cv2.COLOR_BGRA2BGR))
        return self._derive(pixels.copy())

    def to_rgba(self) -> OpenCVImage:
        pixels = self._decoded()
        channels = self.channels
        if channels == _GRAYSCALE_CHANNELS:
            return self._derive(cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGRA))
        if channels == _RGB_CHANNELS:
            return self._derive(cv2.cvtColor(pixels, cv2.COLOR_BGR2BGRA))
        return self._derive(pixels.copy())

    def with_rounded_corners(self, radius: int) -> OpenCVImage:
        rgba = self.to_rgba()._decoded()
        if radius <= 0:
            return self._derive(rgba)

        height, width = rgba.shape[:2]
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
        return self._derive(rgba)

    def flattened(self, background: tuple[int, int, int]) -> OpenCVImage:
        rgba = self.to_rgba()._decoded()
        value_max = _max_value(rgba.dtype)
        alpha = rgba[:, :, _ALPHA : _ALPHA + 1].astype(np.float64) / value_max
        # background is RGB at 8 bits; pixels are BGR at the image's depth
        red, green, blue = background
        canvas = np.array([blue, green, red], dtype=np.float64) / 255.0 * value_max
        composited = rgba[:, :, :_ALPHA].astype(np.float64) * alpha + canvas * (1.0 - alpha)
        if np.issubdtype(rgba.dtype, np.integer):
            composited = np.round(composited)
        return self._derive(composited.astype(rgba.dtype))

    def encode(
        self,
        image_format: str,
        *,
        quality: int | None = None,
        optimize: bool = False,  # noqa: ARG002 - OpenCV has no equivalent
        exif_bytes: bytes | None = None,
        text_metadata: dict[str, str] | None = None,
    ) -> bytes:
        target = normalize_format_name(image_format)
        extension = self._ENCODE_EXTENSIONS.get(target)
        if extension is None:
            msg = f"Attempted to save an image as {target}. Failed because the OpenCV image backend cannot write that format."
            raise ValueError(msg)

        pixels = self._decoded()
        if target in self._EIGHT_BIT_FORMATS:
            pixels = _to_eight_bit(pixels)

        params = self._encode_params(target, quality)
        if exif_bytes is not None and target in self._EXIF_FORMATS:
            ok, buffer = cv2.imencodeWithMetadata(
                extension, pixels, [cv2.IMAGE_METADATA_EXIF], [np.frombuffer(exif_bytes, np.uint8)], params
            )
        else:
            ok, buffer = cv2.imencode(extension, pixels, params)
        if not ok:
            msg = f"Attempted to save an image as {target}. Failed because OpenCV could not encode it."
            raise ValueError(msg)

        encoded = buffer.tobytes()
        if text_metadata and target == "PNG":
            encoded = insert_text_chunks(encoded, text_metadata)
        return encoded

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

        # IMREAD_UNCHANGED keeps bit depth and alpha, and leaves EXIF orientation to exif_transposed
        pixels, metadata_types, metadata = cv2.imdecodeWithMetadata(
            np.frombuffer(self._source, np.uint8), cv2.IMREAD_UNCHANGED
        )
        if pixels is None or pixels.size == 0:
            msg = "Attempted to decode an image. Failed because OpenCV does not recognize the data as an image."
            raise ValueError(msg)

        for metadata_type, block in zip(metadata_types, metadata, strict=True):
            if metadata_type == cv2.IMAGE_METADATA_EXIF:
                self._exif = bytes(block).removeprefix(_EXIF_HEADER)
        self._pixels = pixels
        return pixels

    def _derive(self, pixels: np.ndarray) -> OpenCVImage:
        return OpenCVImage(source=None, pixels=pixels, image_format=self._format)

    def _encode_params(self, target: str, quality: int | None) -> list[int]:
        if quality is None:
            quality = self._DEFAULT_QUALITY.get(target)
        if quality is None:
            return []
        if target == "JPEG":
            return [cv2.IMWRITE_JPEG_QUALITY, quality]
        if target == "WEBP":
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


def _max_value(dtype: np.dtype) -> float:
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return 1.0


def _to_eight_bit(pixels: np.ndarray) -> np.ndarray:
    if pixels.dtype == np.uint8:
        return pixels
    scaled = pixels.astype(np.float64) / _max_value(pixels.dtype) * 255.0
    return np.clip(np.round(scaled), 0, 255).astype(np.uint8)


def _apply_orientation(pixels: np.ndarray, orientation: int) -> np.ndarray:  # noqa: PLR0911
    """Rotate and flip ``pixels`` for an EXIF orientation value (1-8)."""
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


def _exif_orientation(exif: bytes | None) -> int:
    """Read the orientation tag from IFD0 of a TIFF-structured EXIF block. 1 (upright) if absent."""
    if exif is None or len(exif) < 8:  # noqa: PLR2004
        return 1
    if exif[:2] == b"II":
        endian = "<"
    elif exif[:2] == b"MM":
        endian = ">"
    else:
        return 1

    (ifd_offset,) = struct.unpack(f"{endian}I", exif[4:8])
    if ifd_offset + 2 > len(exif):
        return 1
    (entry_count,) = struct.unpack(f"{endian}H", exif[ifd_offset : ifd_offset + 2])
    for index in range(entry_count):
        entry_start = ifd_offset + 2 + index * _IFD_ENTRY_BYTES
        entry = exif[entry_start : entry_start + _IFD_ENTRY_BYTES]
        if len(entry) < _IFD_ENTRY_BYTES:
            return 1
        tag, _value_type, _count = struct.unpack(f"{endian}HHI", entry[:8])
        if tag == _EXIF_ORIENTATION_TAG:
            (orientation,) = struct.unpack(f"{endian}H", entry[8:10])
            return orientation
    return 1
