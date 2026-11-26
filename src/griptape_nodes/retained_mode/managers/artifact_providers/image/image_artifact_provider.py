"""Image artifact provider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageOps

from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import pillow
from griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider import (
    BaseImageArtifactProvider,
    ImageArtifactMetadata,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

logger = logging.getLogger("griptape_nodes")

__all__ = ["ImageArtifactMetadata", "ImageArtifactProvider"]


class ImageArtifactProvider(BaseImageArtifactProvider):
    """Provider for image artifacts using Pillow for standard thumbnails and source metadata."""

    def __init__(self, registry: ProviderRegistry, engine: Engine | None = None) -> None:
        """Initialize the image artifact provider.

        Args:
            registry: The ProviderRegistry that manages this provider
            engine: The engine this provider belongs to
        """
        super().__init__(registry, engine)
        logger.info("Loaded Image artifact provider: Pillow")

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return pillow.get_supported_source_formats()

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> ImageArtifactMetadata | None:
        """Extract original image metadata via PIL's lazy header read (no full decode)."""
        try:
            path = Path(source_path)
            with Image.open(path) as raw_img:
                img = ImageOps.exif_transpose(raw_img)
                width, height = img.size
                channels, color_space = pillow.get_mode_info(img.mode)
                return ImageArtifactMetadata(
                    width=width,
                    height=height,
                    format=(img.format or path.suffix.lstrip(".")).upper(),
                    channels=channels,
                    color_space=color_space,
                    file_size=path.stat().st_size,
                )
        except Exception:
            return None

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes."""
        # Project templates import this provider, and the generators import events that depend on those templates.
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILRoundedPreviewGenerator,
            PILThumbnailGenerator,
        )

        return [PILThumbnailGenerator, PILRoundedPreviewGenerator]
