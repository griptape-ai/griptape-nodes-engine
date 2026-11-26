"""Base class for image metadata injection and extraction drivers."""

from abc import ABC, abstractmethod
from io import BytesIO

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
    def inject_metadata(self, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:
        """Inject metadata into image and return modified image bytes.

        Args:
            pil_image: PIL Image object to inject metadata into
            metadata: Dictionary of key-value string pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On metadata injection failures
        """
        ...

    def inject_metadata_into_bytes(self, data: bytes, metadata: dict[str, str]) -> bytes:
        """Inject metadata into encoded image bytes.

        The default opens the image with Pillow and delegates to `inject_metadata`, so
        existing custom drivers need no changes. Drivers can override this hook to avoid
        decoding and re-encoding pixels. The source image and buffer are closed on failure
        as well as success; the returned bytes must not depend on either resource.
        """
        with BytesIO(data) as source, Image.open(source) as pil_image:
            return self.inject_metadata(pil_image, metadata)

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
