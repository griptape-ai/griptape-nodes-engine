"""Registry for artifact provider classes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
        BaseArtifactProvider,
    )

logger = logging.getLogger("griptape_nodes")


class ProviderRegistry(EngineScoped):
    """Registry for artifact provider classes with lazy instantiation.

    Manages provider registration, lookup by friendly name or file format,
    and lazy instantiation of provider instances.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        """Initialize empty registry."""
        super().__init__(engine)
        self._provider_classes: list[type[BaseArtifactProvider]] = []
        self._provider_instances: dict[type[BaseArtifactProvider], BaseArtifactProvider] = {}
        self._file_format_to_provider_class: dict[str, list[type[BaseArtifactProvider]]] = {}
        self._friendly_name_to_provider_class: dict[str, type[BaseArtifactProvider]] = {}
        self._provider_preview_generators: dict[type[BaseArtifactProvider], list[type]] = {}

    def register_provider(self, provider_class: type[BaseArtifactProvider]) -> None:
        """Register a provider class.

        Args:
            provider_class: The provider class to register

        Raises:
            ValueError: If provider with same friendly name already registered
            RuntimeError: If provider class methods fail
        """
        provider_name = provider_class.__name__

        # FAILURE CASE: Try to access class methods
        try:
            friendly_name = provider_class.get_friendly_name()
            supported_formats = provider_class.get_supported_formats()
        except Exception as e:
            msg = f"Attempted to register artifact provider {provider_name}. Failed due to: class method access error - {e}"
            raise RuntimeError(msg) from e

        # FAILURE CASE: Check for duplicate friendly name
        friendly_name_lower = friendly_name.lower()
        if friendly_name_lower in self._friendly_name_to_provider_class:
            existing_provider_class = self._friendly_name_to_provider_class[friendly_name_lower]
            msg = (
                f"Attempted to register artifact provider '{friendly_name}' ({provider_name}). "
                f"Failed due to: duplicate friendly name with existing provider ({existing_provider_class.__name__})"
            )
            raise ValueError(msg)

        # SUCCESS PATH: Register provider class
        self._provider_classes.append(provider_class)

        for file_format in supported_formats:
            if file_format not in self._file_format_to_provider_class:
                self._file_format_to_provider_class[file_format] = []
            self._file_format_to_provider_class[file_format].append(provider_class)

        self._friendly_name_to_provider_class[friendly_name_lower] = provider_class

    def get_provider_class_by_friendly_name(self, friendly_name: str) -> type[BaseArtifactProvider] | None:
        """Get provider class by friendly name (case-insensitive).

        Args:
            friendly_name: The friendly name to search for

        Returns:
            The provider class if found, None otherwise
        """
        return self._friendly_name_to_provider_class.get(friendly_name.lower())

    def get_provider_classes_by_format(self, file_format: str) -> list[type[BaseArtifactProvider]]:
        """Get all provider classes that support a given file format.

        Args:
            file_format: The file format to search for

        Returns:
            List of provider classes that support the format (empty list if none found)
        """
        return self._file_format_to_provider_class.get(file_format, [])

    def get_or_create_provider_instance(self, provider_class: type[BaseArtifactProvider]) -> BaseArtifactProvider:
        """Get or create singleton instance of provider class (lazy instantiation).

        Args:
            provider_class: The provider class to instantiate

        Returns:
            Cached singleton instance of the provider

        Raises:
            RuntimeError: If provider instantiation fails
        """
        if provider_class not in self._provider_instances:
            try:
                self._provider_instances[provider_class] = provider_class(registry=self, engine=self.engine)
            except Exception as e:
                logger.error("Failed to instantiate provider %s: %s", provider_class.__name__, e)
                msg = f"Failed to instantiate provider {provider_class.__name__}: {e}"
                raise RuntimeError(msg) from e

        return self._provider_instances[provider_class]

    def get_all_provider_classes(self) -> list[type[BaseArtifactProvider]]:
        """Get list of all registered provider classes.

        Returns:
            List of all registered provider classes
        """
        return self._provider_classes

    def get_provider_config_schema(self, provider_class: type[BaseArtifactProvider]) -> dict:
        """Generate config schema for a provider.

        Args:
            provider_class: The provider class to generate config schema for

        Returns:
            Dictionary mapping config keys to default values
        """
        format_key = provider_class.get_preview_format_config_key()
        format_value = provider_class.get_default_preview_format()
        generator_key = provider_class.get_preview_generator_config_key()
        generator_value = provider_class.get_default_preview_generator()

        return {
            format_key: format_value,
            generator_key: generator_value,
        }

    def register_preview_generator_with_provider(
        self, provider_class: type[BaseArtifactProvider], preview_generator_class: type
    ) -> None:
        """Register a preview generator class with a provider class (no instantiation).

        Args:
            provider_class: The provider class this preview generator belongs to
            preview_generator_class: The preview generator class to register
        """
        if provider_class not in self._provider_preview_generators:
            self._provider_preview_generators[provider_class] = []

        if preview_generator_class not in self._provider_preview_generators[provider_class]:
            self._provider_preview_generators[provider_class].append(preview_generator_class)

    def get_preview_generators_for_provider(self, provider_class: type[BaseArtifactProvider]) -> list[type]:
        """Get all registered preview generators for a provider class.

        Combines default preview generators from the provider class with any dynamically registered preview generators.

        Args:
            provider_class: The provider class to get preview generators for

        Returns:
            List of all preview generator classes for this provider
        """
        preview_generators = provider_class.get_default_preview_generators().copy()

        if provider_class in self._provider_preview_generators:
            for preview_generator_class in self._provider_preview_generators[provider_class]:
                if preview_generator_class not in preview_generators:
                    preview_generators.append(preview_generator_class)

        return preview_generators

    def get_preview_generator_by_name(
        self, provider_class: type[BaseArtifactProvider], friendly_name: str
    ) -> type | None:
        """Get a specific preview generator by name for a provider.

        Args:
            provider_class: The provider class to get preview generator for
            friendly_name: The friendly name to search for (case-insensitive)

        Returns:
            The preview generator class if found, None otherwise
        """
        generators = self.get_preview_generators_for_provider(provider_class)
        for gen_class in generators:
            if gen_class.get_friendly_name().lower() == friendly_name.lower():
                return gen_class
        return None
