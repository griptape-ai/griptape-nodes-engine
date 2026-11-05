"""The image interface the engine decodes and encodes images through."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, Self

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from types import TracebackType

    from griptape_nodes.image.formats import ImageFormat
    from griptape_nodes.image.metadata import ImageMetadata
    from griptape_nodes.image.pipeline import PipelineStep

# Bounds ``GriptapeImagePipeline.thumbnail`` shrinks to by default; the preview generators' default size
DEFAULT_THUMBNAIL_SIZE = 1024


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


class GriptapeImage(ABC):
    """A decoded image, independent of the library that decoded it.

    Instances are immutable. Transform one with ``GriptapeImagePipeline``, which returns a new
    image. Only an image opened from a source carries the source's ``metadata``; pipeline output
    keeps ``format`` but has empty metadata, since the source's may no longer describe the pixels
    (EXIF orientation after an EXIF transpose, for example).

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
    def format(self) -> ImageFormat | None:
        """Source container format, or None if it is not one ``ImageFormat`` names."""

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
    def pixel_type(self) -> PixelType:
        """Data type of each channel of the decoded pixels."""

    @property
    @abstractmethod
    def metadata(self) -> ImageMetadata:
        """The source's text and EXIF metadata. Empty for an image a pipeline produced."""

    @abstractmethod
    def close(self) -> None:
        """Release the source file and decoded pixels."""

    @abstractmethod
    def encode(
        self,
        image_format: ImageFormat,
        *,
        quality: int | None = None,
        optimize: bool = False,
        metadata: ImageMetadata | None = None,
    ) -> bytes:
        """Encode the image.

        Args:
            image_format: Target format
            quality: Lossy quality 1-100 for JPEG/WEBP; None uses the format's default
            optimize: Spend extra time for a smaller file where the format supports it
            metadata: Metadata to embed; the parts ``image_format`` has no place for are dropped.
                The image's own ``metadata`` is not written unless passed here.

        Returns:
            The encoded file bytes
        """

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

    @abstractmethod
    def _apply_step(self, step: PipelineStep | None) -> GriptapeImage:
        """Return a new image with ``step`` applied, or an unchanged copy for None.

        Called with the opened source first, so a backend can shortcut the decode (decode a JPEG
        at reduced size for a thumbnail, say).
        """

    @classmethod
    @abstractmethod
    def open(cls, source: bytes | Path) -> GriptapeImage:
        """Open an image from encoded bytes or a file path.

        Backends may defer decoding until pixels are first needed, so a decode error can surface
        from a later call instead of from ``open``.
        """
