"""Select the built-in image provider for the engine environment."""

from griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider import (
    BaseImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider

try:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.openimageio_artifact_provider import (
        OpenImageIOArtifactProvider,
    )
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    _IMAGE_ARTIFACT_PROVIDER_CLASS: type[BaseImageArtifactProvider] = ImageArtifactProvider
else:
    _IMAGE_ARTIFACT_PROVIDER_CLASS = OpenImageIOArtifactProvider


def get_image_artifact_provider_class() -> type[BaseImageArtifactProvider]:
    """Return the built-in provider class selected for this process."""
    return _IMAGE_ARTIFACT_PROVIDER_CLASS
