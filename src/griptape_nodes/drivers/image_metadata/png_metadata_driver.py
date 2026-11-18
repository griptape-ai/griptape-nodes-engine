"""PNG metadata injection and extraction driver using text chunks."""

from __future__ import annotations

from dataclasses import replace

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import (
    BaseImageMetadataDriver,
    MetadataImageInput,
    opening_metadata_image,
)
from griptape_nodes.image import ImageFormat


class PngMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for PNG metadata using text chunks.

    Supports reading and writing PNG text chunks (`tEXt`, `zTXt`, `iTXt`), preserving existing
    text chunks, EXIF, and ICC color profiles when injecting new metadata.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of format strings this driver supports."""
        return ["PNG"]

    def inject_metadata(self, image: MetadataImageInput, metadata: dict[str, str]) -> bytes:
        """Inject metadata into PNG text chunks, overwriting matching keys and keeping the rest."""
        with opening_metadata_image(image) as gt_image:
            text_metadata = {key: value for key, value in gt_image.metadata.text.items() if key not in metadata}
            for key, value in metadata.items():
                text_metadata[key] = str(value)
            return gt_image.encode(ImageFormat.PNG, metadata=replace(gt_image.metadata, text=text_metadata))

    def extract_metadata(self, image: MetadataImageInput) -> dict[str, str]:
        """Extract all text chunks from a PNG image."""
        with opening_metadata_image(image) as gt_image:
            return dict(gt_image.metadata.text)
