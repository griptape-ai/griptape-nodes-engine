"""Base class for image metadata injection and extraction drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from griptape_nodes.image import GriptapeImage


class BaseImageMetadataDriver(ABC):
    """Base class for bidirectional image metadata drivers.

    Each driver handles a specific metadata protocol (e.g., PNG text chunks, EXIF).
    Drivers support both injection (writing) and extraction (reading) of metadata.

    Extraction returns ALL available metadata for the format.
    Injection writes only custom key-value pairs, preserving existing metadata.

    Drivers are registered with ImageMetadataDriverRegistry and selected based on image format.
    """

    @abstractmethod
    def get_supported_formats(self) -> list[str]:
        """Return list of GriptapeImage format names this driver supports.

        Returns:
            List of format strings (e.g., ["PNG"], ["JPEG", "TIFF", "MPO"])
        """
        ...

    @abstractmethod
    def inject_metadata(self, image: GriptapeImage, metadata: dict[str, str]) -> bytes:
        """Inject metadata into image and return modified image bytes.

        Args:
            image: Source image to inject metadata into
            metadata: Dictionary of key-value string pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On metadata injection failures
        """
        ...

    @abstractmethod
    def extract_metadata(self, image: GriptapeImage) -> dict[str, str]:
        """Extract ALL metadata from image.

        Returns all available metadata for the format:
        - PNG: All text chunks
        - EXIF: Standard tags, GPS data, and custom UserComment field

        The amount and type of metadata depends on the format and what's
        present in the image. Custom metadata injected via inject_metadata()
        is included alongside format-specific metadata.

        Args:
            image: Source image to extract metadata from

        Returns:
            Dictionary of metadata key-value pairs, empty dict if none found
        """
        ...
