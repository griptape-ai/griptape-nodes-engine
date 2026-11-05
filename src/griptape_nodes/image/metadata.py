"""Image metadata independent of container format."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.image.exif import (
    EXIF_GPSINFO_TAG,
    EXIF_ORIENTATION_TAG,
    TRANSPOSING_ORIENTATIONS,
    Exif,
    ExifRational,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "EXIF_GPSINFO_TAG",
    "EXIF_ORIENTATION_TAG",
    "TRANSPOSING_ORIENTATIONS",
    "Exif",
    "ExifRational",
    "ImageMetadata",
]


@dataclass(frozen=True)
class ImageMetadata:
    """Metadata carried alongside an image's pixels.

    ``ImageMetadata()`` is no metadata at all: passed to ``GriptapeImage.encode``, it writes none.

    Attributes:
        text: Key/value text, such as an embedded workflow. Stored as PNG text chunks; formats
            with no place for it drop it on encode.
        exif: EXIF tags, or None if the image has none. Formats with no EXIF support drop it.
        icc_profile: The ICC color profile describing the pixels' color space, or None if the
            image is untagged. Formats with no place for a profile drop it.
    """

    text: Mapping[str, str] = field(default_factory=dict)
    exif: Exif | None = None
    icc_profile: bytes | None = None

    def is_empty(self) -> bool:
        """True if there is no text, EXIF, or color profile to write."""
        return not self.text and not self.exif and self.icc_profile is None
