"""Image metadata independent of container format and image backend."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PIL import Image

if TYPE_CHECKING:
    from collections.abc import ItemsView, Mapping

EXIF_ORIENTATION_TAG = 0x0112
EXIF_GPSINFO_TAG = 0x8825

# EXIF orientations that turn the image a quarter turn (or transpose it), swapping width and height
TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})

_EXIF_HEADER = b"Exif\x00\x00"
_UPRIGHT = 1
_MAX_ORIENTATION = 8


class Exif:
    """Parsed EXIF tags, keyed by the tag numbers the EXIF and TIFF specs assign.

    Values come back parsed: ``str``, ``int``, rationals, tuples, and ``bytes`` for undefined
    data. Tags this class doesn't change survive a read-modify-write untouched, maker notes and
    unknown tags included.
    """

    def __init__(self) -> None:
        # Pillow is a dependency of every backend; its parser keeps unknown tags intact
        self._tags = Image.Exif()

    def __bool__(self) -> bool:
        return bool(self._tags)

    def __contains__(self, tag: int) -> bool:
        return tag in self._tags

    def __getitem__(self, tag: int) -> Any:
        return self._tags[tag]

    def __setitem__(self, tag: int, value: Any) -> None:
        self._tags[tag] = value

    @property
    def orientation(self) -> int:
        """The Orientation tag (1-8), or 1 (upright) when absent or invalid."""
        orientation = self._tags.get(EXIF_ORIENTATION_TAG, _UPRIGHT)
        if not isinstance(orientation, int) or not _UPRIGHT <= orientation <= _MAX_ORIENTATION:
            return _UPRIGHT
        return orientation

    @property
    def swaps_dimensions(self) -> bool:
        """True if displaying the image upright swaps its width and height."""
        return self.orientation in TRANSPOSING_ORIENTATIONS

    @property
    def gps(self) -> dict[int, Any]:
        """Tags of the GPS sub-IFD, keyed by GPS tag number. Empty if there is none."""
        return dict(self._tags.get_ifd(EXIF_GPSINFO_TAG))

    def get(self, tag: int, default: Any = None) -> Any:
        return self._tags.get(tag, default)

    def items(self) -> ItemsView[int, Any]:
        """Tags of the primary IFD."""
        return self._tags.items()

    def to_bytes(self) -> bytes:
        """Serialize to a TIFF-structured EXIF block, the form containers store. For backends."""
        return self._tags.tobytes().removeprefix(_EXIF_HEADER)

    @classmethod
    def from_bytes(cls, block: bytes) -> Exif:
        """Parse a TIFF-structured EXIF block, with or without the leading "Exif" marker. For backends.

        A block that isn't valid TIFF parses as empty, as Pillow treats it when it opens an image.
        """
        exif = cls()
        try:
            exif._tags.load(block.removeprefix(_EXIF_HEADER))
        except (SyntaxError, ValueError, struct.error):
            return cls()
        return exif


@dataclass(frozen=True)
class ImageMetadata:
    """Metadata carried alongside an image's pixels.

    Attributes:
        text: Key/value text, such as an embedded workflow. Stored as PNG text chunks; formats
            with no place for it drop it on encode.
        exif: EXIF tags, or None if the image has none. Formats with no EXIF support drop it.
    """

    text: Mapping[str, str] = field(default_factory=dict)
    exif: Exif | None = None
