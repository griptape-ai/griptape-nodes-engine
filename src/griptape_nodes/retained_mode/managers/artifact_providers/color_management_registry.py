"""Registry for the single colour-management provider class."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_color_management_provider import (
        BaseColorManagementProvider,
    )

logger = logging.getLogger("griptape_nodes")


class ColorManagementRegistry(EngineScoped):
    """Registry for the colour-management provider class.

    First-registered-wins: a second registration is not an error, it just logs
    a warning and keeps the first provider. Unlike ``ProviderRegistry``, there
    is no lazy instantiation here -- callers only need the provider class to
    call its classmethods.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        """Initialize with no provider registered."""
        super().__init__(engine)
        self._provider_class: type[BaseColorManagementProvider] | None = None

    def register_provider(self, provider_class: type[BaseColorManagementProvider]) -> None:
        """Register a colour-management provider class.

        Args:
            provider_class: The provider class to register.
        """
        if self._provider_class is not None:
            logger.warning(
                "Colour-management provider '%s' (%s) already registered; ignoring %s.",
                self._provider_class.get_friendly_name(),
                self._provider_class.__name__,
                provider_class.__name__,
            )
            return

        self._provider_class = provider_class

    def get_registered_provider(self) -> type[BaseColorManagementProvider] | None:
        """Get the registered colour-management provider class, if any.

        Returns:
            The registered provider class, or None if nothing is registered.
        """
        return self._provider_class
