"""Base class for image metadata injection and extraction drivers."""

from abc import ABC, abstractmethod

from PIL import Image


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
        """Return list of PIL format strings this driver supports.

        Returns:
            List of format strings (e.g., ["PNG"], ["JPEG", "TIFF", "MPO"])
        """
        ...

    @abstractmethod
    def inject_metadata(self, data: bytes, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:
        """Inject metadata into image and return modified image bytes.

        Args:
            data: Encoded image bytes that pil_image was opened from. A driver that can add
                metadata without re-encoding the image works from these bytes.
            pil_image: PIL Image object opened from data
            metadata: Dictionary of key-value string pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On metadata injection failures
        """
        ...

    @abstractmethod
    def extract_metadata(self, pil_image: Image.Image) -> dict[str, str]:
        """Extract ALL metadata from image.

        Returns all available metadata for the format:
        - PNG: All text chunks
        - EXIF: Standard tags, GPS data, and custom UserComment field

        The amount and type of metadata depends on the format and what's
        present in the image. Custom metadata injected via inject_metadata()
        is included alongside format-specific metadata.

        Args:
            pil_image: PIL Image object to extract metadata from

        Returns:
            Dictionary of metadata key-value pairs, empty dict if none found
        """
        ...
