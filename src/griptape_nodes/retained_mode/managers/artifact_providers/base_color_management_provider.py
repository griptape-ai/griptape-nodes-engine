"""Base abstract class for colour-management providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped

if TYPE_CHECKING:
    import numpy as np

    from griptape_nodes.retained_mode.events.base_events import RequestPayload


class BaseColorManagementProvider(EngineScoped, ABC):
    """Abstract base class for colour-management capability providers.

    A colour-management provider exposes an OCIO-style display/view transform
    plus introspection of the colourspaces/displays/views a config offers.
    Unlike ``BaseArtifactProvider``, this contract has no file decode/encode
    surface -- it is purely about colour transforms.
    """

    @classmethod
    @abstractmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name for this colour-management provider.

        Returns:
            The friendly name for this provider (e.g., 'OpenColorIO')
        """
        ...

    @classmethod
    @abstractmethod
    def build_colorspace_transform_request(
        cls,
        pixels: np.ndarray,
        source_colorspace: str,
        *,
        display: str = "",
        view: str = "",
        config_path: str | None = None,
    ) -> RequestPayload:
        """Build the request a caller fires via GriptapeNodes.handle_request() to perform this transform.

        Args:
            pixels: The pixel data to transform.
            source_colorspace: The colourspace the pixel data is currently in.
            display: The OCIO display to transform to.
            view: The OCIO view to transform to.
            config_path: Path to the OCIO config to use, or None for the default.

        Returns:
            A RequestPayload the caller dispatches to perform the transform.
        """
        ...

    @classmethod
    @abstractmethod
    def list_colorspaces(cls, config_path: str | None) -> list[str]:
        """List the colourspaces available in the given OCIO config.

        Args:
            config_path: Path to the OCIO config to inspect, or None for the default.
        """
        ...

    @classmethod
    @abstractmethod
    def list_displays(cls, config_path: str | None) -> list[str]:
        """List the displays available in the given OCIO config.

        Args:
            config_path: Path to the OCIO config to inspect, or None for the default.
        """
        ...

    @classmethod
    @abstractmethod
    def list_views(cls, config_path: str | None, display: str) -> list[str]:
        """List the views available for a display in the given OCIO config.

        Args:
            config_path: Path to the OCIO config to inspect, or None for the default.
            display: The display to list views for.
        """
        ...
