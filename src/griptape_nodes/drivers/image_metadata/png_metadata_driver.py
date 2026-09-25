"""PNG metadata injection and extraction driver using text chunks."""

from io import BytesIO

from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver

# Text longer than this, such as an embedded workflow, is stored compressed. Shorter text stays
# uncompressed, where image viewers that only show plain text chunks can still read it.
_COMPRESS_TEXT_LONGER_THAN = 1024


class PngMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for PNG metadata using text chunks.

    Supports both reading and writing metadata in PNG text chunks.
    Preserves existing text chunks when writing new metadata.
    All PNG-specific logic is encapsulated in this driver.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of PIL format strings this driver supports.

        Returns:
            List containing "PNG"
        """
        return ["PNG"]

    def inject_metadata(self, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:
        """Inject metadata into PNG text chunks.

        Creates PngInfo with existing text chunks and adds new metadata.
        New metadata overwrites existing keys with same name.

        Args:
            pil_image: PIL Image to inject metadata into
            metadata: Dictionary of key-value pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On PNG save errors
        """
        # Create PNG info object
        png_info = PngImagePlugin.PngInfo()

        # Preserve existing text chunks
        for key, value in pil_image.info.items():
            # Only preserve string key-value pairs (text chunks), skip binary data
            # Don't preserve keys that will be overwritten by new metadata
            if isinstance(key, str) and isinstance(value, str) and key not in metadata:
                png_info.add_text(key, value, zip=len(value) > _COMPRESS_TEXT_LONGER_THAN)

        # Add new metadata
        for key, value in metadata.items():
            text = str(value)
            png_info.add_text(key, text, zip=len(text) > _COMPRESS_TEXT_LONGER_THAN)

        # Save with metadata
        output_buffer = BytesIO()
        pil_image.save(output_buffer, format="PNG", pnginfo=png_info)
        return output_buffer.getvalue()

    def extract_metadata(self, pil_image: Image.Image) -> dict[str, str]:
        """Extract all text chunks from PNG image.

        Args:
            pil_image: PIL Image to extract metadata from

        Returns:
            Dictionary of all PNG text chunks, empty dict if none found
        """
        # PIL unpacks PNG text chunks directly into pil_image.info
        return {key: value for key, value in pil_image.info.items() if isinstance(key, str) and isinstance(value, str)}
