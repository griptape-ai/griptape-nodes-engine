"""Image artifact provider backed by OpenImageIO."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.drivers.image_metadata.image_metadata_driver_registry import (
    ImageMetadataDriverRegistry,
)
from griptape_nodes.files.media_variables import MediaInfo
from griptape_nodes.image import SUPPORTED_IMAGE_EXTENSIONS, GriptapeImage
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

    def to_media_info(self) -> MediaInfo:
        return MediaInfo(
            width=self.width,
            height=self.height,
            channels=self.channels,
            color_space=self.color_space,
        )


class ImageArtifactProvider(BaseArtifactProvider):
    """Provider for image artifacts decoded via OpenImageIO."""

    def __init__(self, registry: ProviderRegistry, engine: Engine | None = None) -> None:
        super().__init__(registry, engine)

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

            driver = ImageMetadataDriverRegistry.get_driver_for_format(str(image.format))
            if driver is None:
                logger.warning("No metadata driver found for format: %s", image.format)
                return data

            try:
                return driver.inject_metadata(image, metadata)
            except Exception as e:
                logger.warning("Attempted to inject metadata into %s. Failed because: %s", file_name, e)
                return data

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Image"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return set(SUPPORTED_IMAGE_EXTENSIONS)

    @classmethod
    def supports_file_extension(cls, file_extension: str) -> bool:
        """Return True if the given file extension (with or without leading dot) is a supported image format."""
        return file_extension.lstrip(".").lower() in cls.get_supported_formats()

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> ImageArtifactMetadata | None:
        """Extract displayed dimensions, format, channels, and color model from the file header."""
        try:
            path = Path(source_path)
            with GriptapeImage.open(path) as img:
                width, height = img.oriented_size
                image_format = str(img.format) if img.format is not None else path.suffix.lstrip(".").upper()
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
        # Lazy import breaks circular dependency:
        # common.project_templates -> ImageArtifactProvider -> preview_generators -> os_events -> project_events -> common.project_templates
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            RoundedPreviewGenerator,
            ThumbnailGenerator,
        )

        return [ThumbnailGenerator, RoundedPreviewGenerator]

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:
        """Magic-byte sniff for supported image formats."""
        return sniff_image_extension(data)

    @classmethod
    def get_metadata_formats(cls) -> set[str]:
        """File extensions that support automatic metadata injection."""
        return {"png"}
