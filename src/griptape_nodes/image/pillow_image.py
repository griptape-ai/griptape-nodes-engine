"""GriptapeImage backed by Pillow."""

from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from PIL import Image, ImageDraw, PngImagePlugin

from griptape_nodes.image.formats import ImageFormat
from griptape_nodes.image.griptape_image import GriptapeImage, PixelType
from griptape_nodes.image.metadata import Exif, ImageMetadata
from griptape_nodes.image.pipeline import Flatten, Orient, PipelineStep, RoundCorners, Thumbnail, ToRGB, ToRGBA
from griptape_nodes.image.png_text_chunks import should_compress_text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class _ModeInfo(NamedTuple):
    channels: int
    color_space: str


class PillowImage(GriptapeImage):
    """GriptapeImage over a ``PIL.Image.Image``.

    Pillow has no 16-bit-per-channel color mode: 16-bit RGB/RGBA sources decode to 8 bits, so
    ``pixel_type`` reports UINT8 for them. 16-bit grayscale (``I;16``) is kept.
    """

    # Pillow mode -> (channels, color_space)
    _MODE_INFO: ClassVar[dict[str, _ModeInfo]] = {
        "1": _ModeInfo(1, "Grayscale"),
        "L": _ModeInfo(1, "Grayscale"),
        "P": _ModeInfo(1, "Palette"),
        "RGB": _ModeInfo(3, "RGB"),
        "RGBA": _ModeInfo(4, "RGBA"),
        "CMYK": _ModeInfo(4, "CMYK"),
        "YCbCr": _ModeInfo(3, "YCbCr"),
        "LAB": _ModeInfo(3, "LAB"),
        "HSV": _ModeInfo(3, "HSV"),
        "I": _ModeInfo(1, "Grayscale"),
        "I;16": _ModeInfo(1, "Grayscale"),
        "I;16B": _ModeInfo(1, "Grayscale"),
        "I;16L": _ModeInfo(1, "Grayscale"),
        "F": _ModeInfo(1, "Grayscale"),
        "LA": _ModeInfo(2, "Grayscale+Alpha"),
        "RGBa": _ModeInfo(4, "RGBA"),  # spellchecker:disable-line
        "RGBX": _ModeInfo(4, "RGB"),
    }

    # Pillow mode -> pixel type, for modes that are not 8-bit. Mode "1" stores a bit per pixel
    # but reads back as 0/255 bytes, so it counts as UINT8.
    _MODE_PIXEL_TYPE: ClassVar[dict[str, PixelType]] = {
        "I": PixelType.INT32,
        "F": PixelType.FLOAT,
        "I;16": PixelType.UINT16,
        "I;16B": PixelType.UINT16,
        "I;16L": PixelType.UINT16,
    }

    # Image.thumbnail's default: draft-decode to no less than twice the target size, then resample
    _REDUCING_GAP = 2.0

    # EXIF orientation -> the transpose that displays it upright, as ImageOps.exif_transpose maps it
    _ORIENTATION_TRANSPOSE: ClassVar[dict[int, Image.Transpose]] = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }

    def __init__(self, image: Image.Image, image_format: ImageFormat | None, *, source: bytes | Path | None) -> None:
        self._image = image
        self._format = image_format
        # Set only for an image opened from a source, which is what carries metadata
        self._source = source

    @property
    def width(self) -> int:
        return self._image.width

    @property
    def height(self) -> int:
        return self._image.height

    @property
    def format(self) -> ImageFormat | None:
        return self._format

    @property
    def channels(self) -> int:
        return self._mode_info().channels

    @property
    def color_space(self) -> str:
        return self._mode_info().color_space

    @property
    def pixel_type(self) -> PixelType:
        return self._MODE_PIXEL_TYPE.get(self._image.mode, PixelType.UINT8)

    @property
    def metadata(self) -> ImageMetadata:
        if self._source is None:
            return ImageMetadata()
        text = {}
        if self._format == ImageFormat.PNG:
            # Pillow unpacks PNG text chunks directly into info
            text = {
                key: value for key, value in self._image.info.items() if isinstance(key, str) and isinstance(value, str)
            }
        pillow_exif = self._image.getexif()
        exif = None
        if pillow_exif:
            exif = Exif.from_bytes(pillow_exif.tobytes())
        return ImageMetadata(text=text, exif=exif)

    def close(self) -> None:
        self._image.close()

    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,
        metadata: ImageMetadata | None = None,
    ) -> bytes:
        save_options: dict[str, Any] = {}
        if quality is not None:
            save_options["quality"] = quality
        if optimize:
            save_options["optimize"] = True
        if metadata is not None and metadata.exif is not None:
            exif = Image.Exif()
            exif.load(metadata.exif.to_bytes())
            save_options["exif"] = exif
        if metadata is not None and metadata.text and image_format == ImageFormat.PNG:
            save_options["pnginfo"] = self._png_info(metadata.text)

        buffer = BytesIO()
        self._image.save(buffer, format=image_format.value, **save_options)
        return buffer.getvalue()

    def _apply_step(self, step: PipelineStep | None) -> PillowImage:  # noqa: PLR0911
        if step is None:
            return self._derive(self._image.copy())
        if isinstance(step, Thumbnail):
            return self._derive(self._thumbnail(step))
        if isinstance(step, Orient):
            return self._derive(self._image.transpose(self._ORIENTATION_TRANSPOSE[step.orientation]))
        if isinstance(step, ToRGB):
            return self._derive(self._image.convert("RGB"))
        if isinstance(step, ToRGBA):
            return self._derive(self._image.convert("RGBA"))
        if isinstance(step, RoundCorners):
            return self._derive(self._rounded(step))
        return self._derive(self._flattened(step))

    def _thumbnail(self, step: Thumbnail) -> Image.Image:
        """Shrink as ``Image.thumbnail`` does, rounding the size in the upright frame for a transposed step."""
        size = self._thumbnail_size(step)
        if size is None:
            return self._image.copy()
        if self._source is None:
            return self._image.resize(size, Image.Resampling.LANCZOS, reducing_gap=self._REDUCING_GAP)

        # A fresh, not-yet-loaded handle, so libjpeg can decode at 1/2, 1/4, or 1/8 size
        # (Image.draft) instead of decoding the full image and shrinking it
        with self._open_pillow(self._source) as handle:
            draft_bounds = (int(step.max_width * self._REDUCING_GAP), int(step.max_height * self._REDUCING_GAP))
            draft = handle.draft(None, draft_bounds)
            box = None
            if draft is not None:
                box = draft[1]
            return handle.resize(size, Image.Resampling.LANCZOS, box=box, reducing_gap=self._REDUCING_GAP)

    def _thumbnail_size(self, step: Thumbnail) -> tuple[int, int] | None:
        """The size ``Image.thumbnail`` shrinks to, or None if the image already fits."""
        if not step.transposed:
            return _fitted_size(self._image.width, self._image.height, step.max_width, step.max_height)
        upright = _fitted_size(self._image.height, self._image.width, step.max_height, step.max_width)
        if upright is None:
            return None
        width, height = upright
        return height, width

    def _rounded(self, step: RoundCorners) -> Image.Image:
        rgba = self._image.convert("RGBA")
        if rgba is self._image:
            rgba = rgba.copy()
        radius = step.radius_pixels(rgba.width, rgba.height)
        if radius <= 0:
            return rgba

        mask = Image.new("L", rgba.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), (rgba.width - 1, rgba.height - 1)], radius=radius, fill=255)
        rgba.putalpha(mask)
        return rgba

    def _flattened(self, step: Flatten) -> Image.Image:
        rgba = self._image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, step.background)
        canvas.paste(rgba, (0, 0), rgba)
        return canvas

    def _mode_info(self) -> _ModeInfo:
        return self._MODE_INFO.get(self._image.mode, _ModeInfo(3, self._image.mode))

    def _derive(self, image: Image.Image) -> PillowImage:
        return PillowImage(image, self._format, source=None)

    @classmethod
    def open(cls, source: bytes | Path) -> PillowImage:
        """Open an image lazily: only the header is read until pixels are needed.

        Raises:
            PIL.UnidentifiedImageError: If the data is not an image Pillow can read
            OSError: If the file cannot be read
        """
        image = cls._open_pillow(source)
        image_format = None
        if image.format is not None:
            image_format = ImageFormat.from_name(image.format)
        return cls(image, image_format, source=source)

    @staticmethod
    def _open_pillow(source: bytes | Path) -> Image.Image:
        if isinstance(source, Path):
            return Image.open(source)
        return Image.open(BytesIO(source))

    @staticmethod
    def _png_info(text: Mapping[str, str]) -> PngImagePlugin.PngInfo:
        png_info = PngImagePlugin.PngInfo()
        for key, value in text.items():
            png_info.add_text(key, value, zip=should_compress_text(value))
        return png_info


def _fitted_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int] | None:
    """Fit ``width`` x ``height`` within the bounds with ``Image.thumbnail``'s rounding, or None if it already fits."""
    if max_width >= width and max_height >= height:
        return None
    aspect = width / height
    if max_width / max_height >= aspect:
        return _round_aspect(max_height * aspect, lambda n: abs(aspect - n / max_height)), max_height
    return max_width, _round_aspect(max_width / aspect, lambda n: _aspect_error(aspect, max_width, n))


def _round_aspect(number: float, key: Callable[[int], float]) -> int:
    return max(min(math.floor(number), math.ceil(number), key=key), 1)


def _aspect_error(aspect: float, width: int, height: int) -> float:
    if height == 0:
        return 0
    return abs(aspect - width / height)
