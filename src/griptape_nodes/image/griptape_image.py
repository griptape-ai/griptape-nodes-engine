"""The image interface the engine decodes, transforms, and encodes images through."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

# Format names follow Pillow's spelling ("PNG", "JPEG", "WEBP", ...), which the metadata
# drivers and callers already key on.
FORMAT_ALIASES = {"JPG": "JPEG", "TIF": "TIFF"}


def normalize_format_name(image_format: str) -> str:
    """Return the canonical upper-case name for a format or file extension ("jpg" -> "JPEG")."""
    upper = image_format.lstrip(".").upper()
    return FORMAT_ALIASES.get(upper, upper)


class GriptapeImage(ABC):
    """A decoded image, independent of the library that decoded it.

    Instances are immutable: every transform returns a new image. Only an image opened from a
    source carries the source's metadata (``text_metadata``, ``exif_bytes``); images returned by
    transforms keep ``format`` but drop metadata, since it may no longer describe the pixels
    (EXIF orientation after ``exif_transposed``, for example).

    Use as a context manager, or call ``close``, to release the source file early.
    """

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
    @abstractmethod
    def width(self) -> int:
        """Width in pixels."""

    @property
    @abstractmethod
    def height(self) -> int:
        """Height in pixels."""

    @property
    @abstractmethod
    def format(self) -> str | None:
        """Source container format ("PNG", "JPEG", ...), or None if it could not be identified."""

    @property
    @abstractmethod
    def channels(self) -> int:
        """Number of channels per pixel, alpha included."""

    @property
    @abstractmethod
    def color_space(self) -> str:
        """Human-readable color model, e.g. "RGB", "RGBA", "Grayscale", "Palette"."""

    @property
    @abstractmethod
    def bit_depth(self) -> int:
        """Bits per channel of the decoded pixels (8, 16, 32)."""

    @property
    @abstractmethod
    def text_metadata(self) -> dict[str, str]:
        """PNG text chunks (tEXt, zTXt, iTXt) of the source, empty for other formats."""

    @property
    @abstractmethod
    def exif_bytes(self) -> bytes | None:
        """The source's EXIF block as TIFF-structured bytes (no leading "Exif" marker), or None."""

    @abstractmethod
    def close(self) -> None:
        """Release the source file and decoded pixels."""

    @abstractmethod
    def exif_transposed(self) -> GriptapeImage:
        """Return the image rotated and flipped as its EXIF orientation tag says to display it."""

    @abstractmethod
    def thumbnail(self, max_width: int, max_height: int) -> GriptapeImage:
        """Return the image shrunk to fit within the bounds, keeping its aspect ratio. Never enlarges."""

    @abstractmethod
    def to_rgb(self) -> GriptapeImage:
        """Return the image as three-channel color, discarding any alpha."""

    @abstractmethod
    def to_rgba(self) -> GriptapeImage:
        """Return the image as four-channel color, adding an opaque alpha if it had none."""

    @abstractmethod
    def with_rounded_corners(self, radius: int) -> GriptapeImage:
        """Return an RGBA copy whose alpha is transparent outside a rectangle with corners of ``radius`` pixels."""

    @abstractmethod
    def flattened(self, background: tuple[int, int, int]) -> GriptapeImage:
        """Return an RGB copy composited over an opaque 8-bit ``background`` color."""

    @abstractmethod
    def encode(
        self,
        image_format: str,
        *,
        quality: int | None = None,
        optimize: bool = False,
        exif_bytes: bytes | None = None,
        text_metadata: dict[str, str] | None = None,
    ) -> bytes:
        """Encode the image.

        Args:
            image_format: Target format or extension ("PNG", "jpg", "webp", ...)
            quality: Lossy quality 1-100 for JPEG/WEBP; None uses the format's default
            optimize: Spend extra time for a smaller file where the format supports it
            exif_bytes: EXIF block to embed, in the same shape ``exif_bytes`` returns
            text_metadata: PNG text chunks to embed; ignored for other formats

        Returns:
            The encoded file bytes
        """

    @classmethod
    @abstractmethod
    def open(cls, source: bytes | Path) -> GriptapeImage:
        """Open an image from encoded bytes or a file path.

        Backends may defer decoding until pixels are first needed, so a decode error can surface
        from a later call instead of from ``open``.
        """
