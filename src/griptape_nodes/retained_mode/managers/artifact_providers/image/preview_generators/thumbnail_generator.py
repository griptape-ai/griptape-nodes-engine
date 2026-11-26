"""Select the standard thumbnail backend once when the engine modules load."""

from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import (
    PILThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_parameters import (
    ThumbnailParameters,
)

try:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import (
        OpenImageIOThumbnailGenerator,
    )
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    ThumbnailGenerator = PILThumbnailGenerator
    THUMBNAIL_BACKEND = "Pillow"
else:
    ThumbnailGenerator = OpenImageIOThumbnailGenerator
    THUMBNAIL_BACKEND = "OpenImageIO"

__all__ = ["THUMBNAIL_BACKEND", "ThumbnailGenerator", "ThumbnailParameters"]
