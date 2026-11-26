"""Image artifact provider with OpenImageIO standard thumbnails."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import openimageio
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
    OpenImageIOThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_rounded_preview_generator import (
    PILRoundedPreviewGenerator,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers import ProviderRegistry
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )

logger = logging.getLogger("griptape_nodes")


class OpenImageIOArtifactProvider(ImageArtifactProvider):
    """Use OpenImageIO for standard thumbnails while retaining Pillow source metadata."""

    def __init__(self, registry: ProviderRegistry, engine: Engine | None = None) -> None:
        super().__init__(registry, engine)
        logger.info("Loaded Image artifact provider: OpenImageIO")

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return openimageio.get_supported_source_formats()

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        return [OpenImageIOThumbnailGenerator, PILRoundedPreviewGenerator]
