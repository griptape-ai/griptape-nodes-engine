"""Image artifact provider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.drivers.image_metadata.image_metadata_driver_registry import (
    ImageMetadataDriverRegistry,
)
from griptape_nodes.image import GriptapeImage
from griptape_nodes.image.formats import sniff_image_extension
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import collect_workflow_metadata
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ImageArtifactMetadata(BaseArtifactMetadata):
    """Metadata extracted from the header of an image source file."""

    width: int
    height: int
    format: str
    channels: int
    color_space: str
    file_size: int


class ImageArtifactProvider(BaseArtifactProvider):
    """Provider for image artifacts.

    Images are decoded through ``griptape_nodes.image``.
    """

    def __init__(self, registry: ProviderRegistry, engine: Engine | None = None) -> None:
        """Initialize the image artifact provider.

        Args:
            registry: The ProviderRegistry that manages this provider
            engine: The engine this provider belongs to
        """
        super().__init__(registry, engine)

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Image"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}

    @classmethod
    def supports_file_extension(cls, file_extension: str) -> bool:
        """Return True if the given file extension (with or without leading dot) is a supported image format."""
        return file_extension.lstrip(".").lower() in cls.get_supported_formats()

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> ImageArtifactMetadata | None:
        """Extract original image metadata: displayed dimensions, format, and color model, from the header alone."""
        try:
            path = Path(source_path)
            with GriptapeImage.open(path) as img:
                # Report the dimensions the image displays at, without decoding or rotating it
                width = img.width
                height = img.height
                exif = img.metadata.exif
                if exif is not None and exif.swaps_dimensions:
                    width, height = height, width
                image_format = path.suffix.lstrip(".").upper()
                if img.format is not None:
                    image_format = str(img.format)
                return ImageArtifactMetadata(
                    width=width,
                    height=height,
                    format=image_format,
                    channels=img.channels,
                    color_space=img.color_space,
                    file_size=path.stat().st_size,
                )
        except Exception:
            return None

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"webp", "jpg", "png"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Standard Thumbnail Generation"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "webp"

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILRoundedPreviewGenerator,
            PILThumbnailGenerator,
        )

        return [PILThumbnailGenerator, PILRoundedPreviewGenerator]

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:
        return sniff_image_extension(data)

    @classmethod
    def get_metadata_formats(cls) -> set[str]:
        """File extensions that support automatic metadata injection.

        Returns:
            Set of lowercase file extensions WITHOUT leading dots
        """
        return {"png"}

    def collect_metadata_for_write(self, file_name: str, workflow: WorkflowRuntime | None) -> dict[str, str]:
        ext = Path(file_name).suffix.lstrip(".").lower()
        if ext not in self.get_metadata_formats():
            return {}

        try:
            return collect_workflow_metadata(self.engine, workflow)
        except Exception as e:
            logger.warning("Attempted to collect workflow metadata for %s. Failed because: %s", file_name, e)
            return {}

    def prepare_content_for_write(self, data: bytes, file_name: str, metadata: dict[str, str]) -> bytes:  # noqa: PLR0911
        if not metadata:
            return data

        if not data:
            logger.warning("Cannot inject metadata: empty data")
            return data

        try:
            image = GriptapeImage.open(data)
        except Exception as e:
            logger.warning("Attempted to open image data for %s. Failed because: %s", file_name, e)
            return data

        with image:
            if image.format is None:
                logger.warning("Could not detect image format from data")
                return data

            driver = ImageMetadataDriverRegistry.get_driver_for_format(image.format)
            if driver is None:
                logger.warning("No metadata driver found for format: %s", image.format)
                return data

            try:
                return driver.inject_metadata(image, metadata)
            except Exception as e:
                logger.warning("Attempted to inject metadata into %s. Failed because: %s", file_name, e)
                return data
