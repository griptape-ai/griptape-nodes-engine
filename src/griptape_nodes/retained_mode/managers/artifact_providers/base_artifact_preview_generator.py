"""Base abstract class for artifact preview generators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.managers.artifact_providers.utils import (
    normalize_friendly_name_to_key,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
        BaseGeneratorParameters,
    )


class BaseArtifactPreviewGenerator(EngineScoped, ABC):
    """Abstract base class for preview generators.

    Preview generators perform the actual conversion work.
    They are passed to provider generate_preview methods.

    Metadata is defined as class methods for zero-cost introspection without instantiation.
    """

    def __init__(
        self,
        source_file_location: str,
        preview_format: str,
        destination_preview_directory: str,
        destination_preview_file_name: str,
        _params: dict[str, Any],
        *,
        engine: Engine | None = None,
    ) -> None:
        """Initialize the preview generator.

        Args:
            source_file_location: Path to the source artifact file
            preview_format: Target format for the preview (e.g., "png", "jpg", "webp")
            destination_preview_directory: Directory where the preview should be saved
            destination_preview_file_name: Filename for the preview
            _params: Generator-specific parameters (validated by subclass __init__)
            engine: The engine whose request bus this generator reads and writes files through

        Note:
            Subclass __init__ must validate _params and set self.params to a Pydantic model instance.
        """
        super().__init__(engine)
        self.source_file_location = source_file_location
        self.preview_format = preview_format
        self.destination_preview_directory = destination_preview_directory
        self.destination_preview_file_name = destination_preview_file_name

    @classmethod
    @abstractmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name for this generator.

        Returns:
            The friendly name for this generator (e.g., 'Pillow Dimension', 'FFmpeg Thumbnail')
        """
        ...

    @classmethod
    @abstractmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process.

        Returns:
            Set of lowercase file extensions WITHOUT leading dots (e.g., {'png', 'jpg'})
        """
        ...

    @classmethod
    @abstractmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces.

        Returns:
            Set of lowercase preview format extensions WITHOUT leading dots (e.g., {'webp', 'jpg'})
        """
        ...

    @classmethod
    @abstractmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class.

        Returns:
            Pydantic model class (subclass of BaseGeneratorParameters)

        Example:
            return PILThumbnailParameters
        """
        ...

    @classmethod
    def get_config_key_prefix(cls, provider_friendly_name: str) -> str:
        """Get the config key prefix for this generator's parameters.

        Args:
            provider_friendly_name: Friendly name of the provider (e.g., 'Image')

        Returns:
            Config key prefix (e.g., 'artifacts.image.preview_generation.preview_generator_configurations.rounded_image_preview_generation')
        """
        provider_key = normalize_friendly_name_to_key(provider_friendly_name)
        generator_key = normalize_friendly_name_to_key(cls.get_friendly_name())
        return f"artifacts.{provider_key}.preview_generation.preview_generator_configurations.{generator_key}"

    @abstractmethod
    async def attempt_generate_preview(self) -> str | dict[str, str]:
        """Attempt to generate preview file(s).

        Writes file(s) to destination directory using provided filename or generated names.

        Returns:
            str: Single filename if one preview generated (e.g., "thumbnail.webp")
            dict[str, str]: Multiple filenames if multiple previews generated
                (e.g., {"mask_r": "output_R.png", "mask_g": "output_G.png"})

        Raises:
            Exception: If preview generation fails

        Examples:
            Single file generator:
                return "thumbnail.webp"

            Multi-file generator:
                return {
                    "mask_r": "output_R.png",
                    "mask_g": "output_G.png",
                    "composite": "composite.png"
                }
        """
        ...
