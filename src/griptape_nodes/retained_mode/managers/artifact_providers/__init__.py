"""Default artifact providers for common media types."""

from griptape_nodes.retained_mode.managers.artifact_providers.audio import AudioArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
    BaseArtifactPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
    BaseGeneratorParameters,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import (
    ProviderRegistry,
)
from griptape_nodes.retained_mode.managers.artifact_providers.utils import (
    normalize_friendly_name_to_key,
)
from griptape_nodes.retained_mode.managers.artifact_providers.video import VideoArtifactProvider

__all__ = [
    "AudioArtifactProvider",
    "BaseArtifactPreviewGenerator",
    "BaseArtifactProvider",
    "BaseGeneratorParameters",
    "ImageArtifactProvider",
    "ProviderRegistry",
    "VideoArtifactProvider",
    "normalize_friendly_name_to_key",
]
