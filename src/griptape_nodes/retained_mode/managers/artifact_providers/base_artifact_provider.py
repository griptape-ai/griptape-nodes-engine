"""Base abstract class for artifact providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from griptape_nodes.retained_mode.managers.artifact_providers.utils import (
    normalize_friendly_name_to_key,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry
    from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial


class BaseArtifactMetadata(BaseModel):
    """Base model for metadata extracted from a source artifact file.

    Subclass and add fields for each artifact type (e.g. ImageArtifactMetadata).
    """


class BaseArtifactProvider(ABC):
    """Abstract base class for artifact type providers.

    Providers define how to handle specific artifact types (images, video, audio)
    including supported formats and preview generation capabilities.

    Metadata is defined as class methods for zero-cost introspection without instantiation.
    Instance attributes may hold heavyweight dependencies (e.g., PIL, ffmpeg) loaded lazily.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        """Initialize provider with registry reference.

        Args:
            registry: The ProviderRegistry that manages this provider
        """
        self._registry = registry

    @classmethod
    @abstractmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name for this artifact type.

        Returns:
            The friendly name for this provider (e.g., 'Image', 'Video', 'Audio')
        """
        ...

    @classmethod
    @abstractmethod
    def get_supported_formats(cls) -> set[str]:
        """File extensions this provider handles.

        Returns:
            Set of lowercase file extensions WITHOUT leading dots (e.g., {'png', 'jpg'})
        """
        ...

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        """Preview formats this provider can generate.

        Override in subclasses that support preview generation. The default
        empty set indicates this provider does not generate previews; the
        artifact manager skips preview-settings registration in that case.

        Returns:
            Set of lowercase preview format extensions WITHOUT leading dots (e.g., {'webp', 'jpg'})
        """
        return set()

    @classmethod
    def get_default_preview_generator(cls) -> str:
        """Default preview generator for this provider.

        Only valid for providers that generate previews. Callers must check
        ``get_preview_formats()`` first; calling this on a provider that
        does not generate previews raises ``NotImplementedError``.

        Returns:
            Friendly name of the default preview generator.
        """
        msg = f"{cls.__name__} does not generate previews; check get_preview_formats() before calling."
        raise NotImplementedError(msg)

    @classmethod
    def get_default_preview_format(cls) -> str:
        """Default preview format for this provider.

        Only valid for providers that generate previews. Callers must check
        ``get_preview_formats()`` first; calling this on a provider that
        does not generate previews raises ``NotImplementedError``.

        Returns:
            Default format extension WITHOUT leading dot (e.g., 'png', 'webp').
        """
        msg = f"{cls.__name__} does not generate previews; check get_preview_formats() before calling."
        raise NotImplementedError(msg)

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes for this provider.

        Only valid for providers that generate previews. Callers must check
        ``get_preview_formats()`` first; calling this on a provider that
        does not generate previews raises ``NotImplementedError``.

        Returns:
            List of default preview generator classes.
        """
        msg = f"{cls.__name__} does not generate previews; check get_preview_formats() before calling."
        raise NotImplementedError(msg)

    @classmethod
    def get_config_key_prefix(cls) -> str:
        """Get the config key prefix for this provider.

        Returns:
            Config key prefix (e.g., 'artifacts.image.preview_generation')
        """
        friendly_name = cls.get_friendly_name()
        provider_key = normalize_friendly_name_to_key(friendly_name)
        return f"artifacts.{provider_key}.preview_generation"

    @classmethod
    def get_preview_format_leaf_key(cls) -> str:
        """Get the leaf key for preview format config.

        Returns:
            The leaf key (e.g., 'preview_format')
        """
        return "preview_format"

    @classmethod
    def get_preview_generator_leaf_key(cls) -> str:
        """Get the leaf key for preview generator config.

        Returns:
            The leaf key (e.g., 'preview_generator')
        """
        return "preview_generator"

    @classmethod
    def get_preview_format_config_key(cls) -> str:
        """Get the config key for the user's selected preview format.

        Returns:
            Config key (e.g., 'artifacts.image.preview_generation.preview_format')
        """
        return f"{cls.get_config_key_prefix()}.{cls.get_preview_format_leaf_key()}"

    @classmethod
    def get_preview_generator_config_key(cls) -> str:
        """Get the config key for the user's selected preview generator.

        Returns:
            Config key (e.g., 'artifacts.image.preview_generation.preview_generator')
        """
        return f"{cls.get_config_key_prefix()}.{cls.get_preview_generator_leaf_key()}"

    async def attempt_generate_preview(  # noqa: PLR0913
        self,
        preview_generator_friendly_name: str,
        source_file_location: str,
        preview_format: str,
        destination_preview_directory: str,
        destination_preview_file_name: str,
        params: dict[str, Any],
    ) -> str | dict[str, str]:
        """Attempt to generate a preview using the specified preview generator.

        This method handles the complete preview generation flow:
        1. Verifies the generator is registered
        2. Validates all required parameters are provided
        3. Verifies the preview format is supported
        4. Instantiates and executes the generator

        Args:
            preview_generator_friendly_name: Friendly name of registered generator to use
            source_file_location: Path to the source artifact file
            preview_format: Target preview format
            destination_preview_directory: Directory where the preview should be saved
            destination_preview_file_name: Filename for the preview
            params: Generator-specific parameters

        Returns:
            Preview filename(s) generated by the generator.

        Raises:
            ValueError: If generator not registered, missing required params, or unsupported format
            Exception: If generator instantiation or execution fails
        """
        # FAILURE CASE: Generator not registered
        generator_class = self._registry.get_preview_generator_by_name(self.__class__, preview_generator_friendly_name)
        if generator_class is None:
            msg = f"Preview generator '{preview_generator_friendly_name}' not registered with this provider"
            raise ValueError(msg)

        # Get generator metadata
        supported_formats = generator_class.get_supported_preview_formats()

        # FAILURE CASE: Verify preview format is supported
        if preview_format not in supported_formats:
            msg = (
                f"Preview format '{preview_format}' not supported by generator "
                f"(supported: {', '.join(sorted(supported_formats))})"
            )
            raise ValueError(msg)

        # FAILURE CASE: Instantiate generator
        generator = generator_class(
            source_file_location, preview_format, destination_preview_directory, destination_preview_file_name, params
        )

        # FAILURE CASE: Execute generator and get result
        result = await generator.attempt_generate_preview()

        # FAILURE CASE: Validate result is not empty dict
        if isinstance(result, dict) and len(result) == 0:
            msg = "Generator returned empty dict - must return at least one file"
            raise ValueError(msg)

        return result

    def prepare_content_for_write(self, data: bytes, file_name: str) -> bytes:  # noqa: ARG002
        """Process content before writing to disk.

        Override in subclasses for format-specific content processing
        (e.g., metadata injection). Default implementation returns data unchanged.

        Args:
            data: Raw file bytes
            file_name: Resolved filename including extension (post-macro resolution and post-policy)

        Returns:
            Processed bytes (or original bytes if no processing needed)
        """
        return data

    def check_write_permission(self, data: bytes, detected_format: str) -> CheckpointDenial | None:  # noqa: ARG002
        """Ask whether writing ``data`` is permitted before it hits disk.

        Override in subclasses that gate writes on media-specific properties
        (e.g., legally-encumbered video codecs). The default implementation
        returns ``None`` (allow), so providers without a policy pay no cost.

        Called by ``OSManager`` after the incoming bytes have been recognized
        as a format this provider claims, and before the disk write occurs.
        The implementation may inspect ``data`` (e.g. run ffprobe on a spooled
        temp file) to extract facts a policy hook cares about, then evaluate
        an ``AuthorizationCheckpoint`` via ``EventManager``.

        Args:
            data: The full buffered write payload.
            detected_format: The lowercase canonical extension the provider
                returned from ``detect_format`` for these bytes (e.g. ``"mp4"``).

        Returns:
            A ``CheckpointDenial`` to refuse the write (OSManager converts it
            to a ``WriteFileResultFailure``), or ``None`` to allow.
        """
        return None

    def check_read_permission(self, source_path: str) -> CheckpointDenial | None:  # noqa: ARG002
        """Ask whether reading the file at ``source_path`` is permitted.

        Override in subclasses that gate reads on media-specific properties.
        The default returns ``None`` (allow); providers without a policy pay
        no cost.

        Called by library code (via
        ``CheckArtifactReadPermissionRequest``) before it hands a file to a
        subprocess or loads it into memory. Implementations typically inspect
        the file (e.g. run ffprobe) to extract facts a policy hook cares
        about, then evaluate an ``AuthorizationCheckpoint`` via
        ``EventManager``.

        Args:
            source_path: Absolute path to the source file. The provider is
                trusted to open it read-only.

        Returns:
            A ``CheckpointDenial`` to refuse the read, or ``None`` to allow.
        """
        return None

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:  # noqa: ARG003
        """Sniff the canonical on-disk extension for ``data`` if this provider recognizes it.

        Override in subclasses to inspect magic bytes for the formats this
        provider handles. The default implementation returns ``None`` (no
        opinion), which lets ``ArtifactManager.sniff_extension`` defer to the
        next provider.

        Args:
            data: Raw file bytes (the head of the buffer is sufficient).

        Returns:
            A lowercase canonical file extension WITHOUT a leading dot
            (e.g. ``"png"``, ``"mp4"``) when the bytes are recognized,
            otherwise ``None``.
        """
        return None

    @classmethod
    @abstractmethod
    def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:
        """Extract metadata from the source file to store alongside the preview.

        Implement in subclasses to return format-specific metadata (e.g., image
        dimensions and channels for images). Return None if not applicable.

        Args:
            source_path: Absolute path to the source file.
        """
