"""PNG metadata injection and extraction driver using text chunks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver
from griptape_nodes.image import ImageFormat, ImageMetadata

if TYPE_CHECKING:
    from griptape_nodes.image import GriptapeImage


class PngMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for PNG metadata using text chunks.

    Supports both reading and writing metadata in PNG text chunks.
    Preserves existing text chunks when writing new metadata.
    All PNG-specific logic is encapsulated in this driver.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of GriptapeImage format names this driver supports.

        Returns:
            List containing "PNG"
        """
        return ["PNG"]

    def inject_metadata(self, image: GriptapeImage, metadata: dict[str, str]) -> bytes:
        """Inject metadata into PNG text chunks.

        Re-encodes the image with its existing text chunks plus the new metadata.
        New metadata overwrites existing keys with same name.

        Args:
            image: Source PNG to inject metadata into
            metadata: Dictionary of key-value pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On PNG save errors
        """
        text_metadata = {key: value for key, value in image.metadata.text.items() if key not in metadata}
        for key, value in metadata.items():
            text_metadata[key] = str(value)
        return image.encode(ImageFormat.PNG, metadata=ImageMetadata(text=text_metadata))

    def extract_metadata(self, image: GriptapeImage) -> dict[str, str]:
        """Extract all text chunks from PNG image.

        Args:
            image: Source PNG to extract metadata from

        Returns:
            Dictionary of all PNG text chunks, empty dict if none found
        """
        return dict(image.metadata.text)
