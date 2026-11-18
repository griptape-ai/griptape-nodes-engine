"""Image preview generators."""

from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.rounded_preview_generator import (
    RoundedParameters,
    RoundedPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator import (
    ThumbnailGenerator,
    ThumbnailParameters,
)

# Backward-compatible aliases for callers importing the historical PIL-prefixed names.
PILRoundedParameters = RoundedParameters
PILRoundedPreviewGenerator = RoundedPreviewGenerator
PILThumbnailGenerator = ThumbnailGenerator
PILThumbnailParameters = ThumbnailParameters

__all__ = [
    "PILRoundedParameters",
    "PILRoundedPreviewGenerator",
    "PILThumbnailGenerator",
    "PILThumbnailParameters",
    "RoundedParameters",
    "RoundedPreviewGenerator",
    "ThumbnailGenerator",
    "ThumbnailParameters",
]
