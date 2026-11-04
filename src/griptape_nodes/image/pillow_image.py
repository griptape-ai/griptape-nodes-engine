"""GriptapeImage backed by Pillow."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from PIL import Image, ImageDraw, ImageOps, PngImagePlugin

from griptape_nodes.image.griptape_image import GriptapeImage, normalize_format_name
from griptape_nodes.image.png_text_chunks import should_compress_text

_EXIF_HEADER = b"Exif\x00\x00"


class _ModeInfo(NamedTuple):
    channels: int
    color_space: str


class PillowImage(GriptapeImage):
    """GriptapeImage over a ``PIL.Image.Image``.

    Pillow has no 16-bit-per-channel color mode: 16-bit RGB/RGBA sources decode to 8 bits, so
    ``bit_depth`` reports 8 for them. 16-bit grayscale (``I;16``) is kept.
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

    # Pillow mode -> bits per channel, for modes that are not 8-bit
    _MODE_BIT_DEPTH: ClassVar[dict[str, int]] = {
        "1": 1,
        "I": 32,
        "F": 32,
        "I;16": 16,
        "I;16B": 16,
        "I;16L": 16,
    }

    def __init__(self, image: Image.Image, image_format: str | None, *, is_source: bool) -> None:
        self._image = image
        self._format = image_format
        self._is_source = is_source

    @property
    def width(self) -> int:
        return self._image.width

    @property
    def height(self) -> int:
        return self._image.height

    @property
    def format(self) -> str | None:
        return self._format

    @property
    def channels(self) -> int:
        return self._mode_info().channels

    @property
    def color_space(self) -> str:
        return self._mode_info().color_space

    @property
    def bit_depth(self) -> int:
        return self._MODE_BIT_DEPTH.get(self._image.mode, 8)

    @property
    def text_metadata(self) -> dict[str, str]:
        if not self._is_source or self._format != "PNG":
            return {}
        # Pillow unpacks PNG text chunks directly into info
        return {
            key: value for key, value in self._image.info.items() if isinstance(key, str) and isinstance(value, str)
        }

    @property
    def exif_bytes(self) -> bytes | None:
        if not self._is_source:
            return None
        exif = self._image.getexif()
        if not exif:
            return None
        return exif.tobytes().removeprefix(_EXIF_HEADER)

    def close(self) -> None:
        self._image.close()

    def exif_transposed(self) -> PillowImage:
        return self._derive(ImageOps.exif_transpose(self._image))

    def thumbnail(self, max_width: int, max_height: int) -> PillowImage:
        resized = self._image.copy()
        resized.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        return self._derive(resized)

    def to_rgb(self) -> PillowImage:
        return self._derive(self._image.convert("RGB"))

    def to_rgba(self) -> PillowImage:
        return self._derive(self._image.convert("RGBA"))

    def with_rounded_corners(self, radius: int) -> PillowImage:
        rgba = self._image.convert("RGBA")
        if rgba is self._image:
            rgba = rgba.copy()
        if radius <= 0:
            return self._derive(rgba)

        mask = Image.new("L", rgba.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), (rgba.width - 1, rgba.height - 1)], radius=radius, fill=255)
        rgba.putalpha(mask)
        return self._derive(rgba)

    def flattened(self, background: tuple[int, int, int]) -> PillowImage:
        rgba = self._image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, (0, 0), rgba)
        return self._derive(canvas)

    def encode(
        self,
        image_format: str,
        *,
        quality: int | None = None,
        optimize: bool = False,
        exif_bytes: bytes | None = None,
        text_metadata: dict[str, str] | None = None,
    ) -> bytes:
        pil_format = normalize_format_name(image_format)
        save_options: dict[str, Any] = {}
        if quality is not None:
            save_options["quality"] = quality
        if optimize:
            save_options["optimize"] = True
        if exif_bytes is not None:
            exif = Image.Exif()
            exif.load(exif_bytes)
            save_options["exif"] = exif
        if text_metadata and pil_format == "PNG":
            save_options["pnginfo"] = self._png_info(text_metadata)

        buffer = BytesIO()
        self._image.save(buffer, format=pil_format, **save_options)
        return buffer.getvalue()

    def _mode_info(self) -> _ModeInfo:
        return self._MODE_INFO.get(self._image.mode, _ModeInfo(3, self._image.mode))

    def _derive(self, image: Image.Image) -> PillowImage:
        return PillowImage(image, self._format, is_source=False)

    @classmethod
    def open(cls, source: bytes | Path) -> PillowImage:
        """Open an image lazily: only the header is read until pixels are needed.

        Raises:
            PIL.UnidentifiedImageError: If the data is not an image Pillow can read
            OSError: If the file cannot be read
        """
        if isinstance(source, Path):
            image = Image.open(source)
        else:
            image = Image.open(BytesIO(source))
        return cls(image, image.format, is_source=True)

    @staticmethod
    def _png_info(text_metadata: dict[str, str]) -> PngImagePlugin.PngInfo:
        png_info = PngImagePlugin.PngInfo()
        for key, text in text_metadata.items():
            png_info.add_text(key, text, zip=should_compress_text(text))
        return png_info
