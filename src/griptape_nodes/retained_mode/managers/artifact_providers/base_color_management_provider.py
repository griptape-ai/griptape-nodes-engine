"""Base abstract class for colour-management providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import EngineScoped

if TYPE_CHECKING:
    import numpy as np

    from griptape_nodes.retained_mode.events.base_events import RequestPayload
    from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
        ImageArtifactSituation,
    )


class ColorTransformTarget:
    """A named, provider-opaque destination for a colour transform.

    Universal callers only need ``identifier`` (to pass back into
    ``build_transform_request``) and ``label`` (to show a human). Everything a specific
    provider needs to actually perform the transform to this target -- an OCIO
    display/view pair, a LUT file path, a gamma value -- lives in ``provider_data`` and
    is never interpreted outside that provider's own implementation.

    Attributes:
        identifier: Opaque, provider-defined string a caller round-trips back into
            ``build_transform_request`` to select this target.
        label: Human-readable name for UI display (e.g. "Rec.709 (sRGB)").
        provider_data: Provider-private detail needed to perform the transform to this
            target. Never inspected by generic callers.
    """

    def __init__(self, identifier: str, label: str, provider_data: dict[str, Any] | None = None) -> None:
        self.identifier = identifier
        self.label = label
        self.provider_data = provider_data or {}


class BaseColorManagementProvider(EngineScoped, ABC):
    """Abstract base class for colour-management capability providers.

    A colour-management provider maps a source colourspace and a transform intent onto
    pixel data, plus offers introspection of what colourspaces and transform targets it
    supports. The contract intentionally has no OCIO vocabulary (no display/view/
    config_path) -- those concepts live entirely inside provider-specific
    ``provider_data`` blobs and provider subclasses, so a non-OCIO backend (a gamma
    curve, a LUT stack, a studio pipeline) can implement this without faking OCIO
    concepts it doesn't have. Unlike ``BaseArtifactProvider``, this contract has no file
    decode/encode surface -- it is purely about colour transforms.
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
    def build_transform_request(
        cls,
        pixels: np.ndarray,
        source_colorspace: str,
        situation: ImageArtifactSituation,
        *,
        provider_data: dict[str, Any] | None = None,
    ) -> RequestPayload:
        """Build the request a caller fires via GriptapeNodes.handle_request() to perform this transform.

        The caller states the source colourspace and what the result is for
        (``situation``, reusing ``ImageArtifactSituation``) rather than a
        provider-specific destination. A provider maps ``situation`` to its own internal
        target (e.g. OCIO maps VIEWER to a configured default display/view pair);
        ``provider_data`` lets a caller override or supplement that mapping with detail
        only that provider understands. Providers must ignore unrecognized keys rather
        than raising, so callers can pass provider_data speculatively without knowing
        which provider is live.

        Args:
            pixels: The pixel data to transform.
            source_colorspace: The colourspace the pixel data is currently in.
            situation: What the transformed result will be used for (e.g. viewer
                display, thumbnail).
            provider_data: Opaque, provider-specific detail this provider may use to
                refine or override its default behaviour for ``situation``. Providers
                must tolerate unknown keys.

        Returns:
            A RequestPayload the caller dispatches to perform the transform.
        """
        ...

    @classmethod
    @abstractmethod
    def list_colorspaces(cls, provider_data: dict[str, Any] | None = None) -> list[str]:
        """List the source colourspaces this provider can transform from.

        Args:
            provider_data: Opaque, provider-specific detail that may affect which
                colourspaces are listed (e.g. an OCIO config path). Providers must
                tolerate unknown keys and a None value.

        Returns:
            The colourspace names this provider recognizes as transform sources.
        """
        ...

    @classmethod
    @abstractmethod
    def list_transform_targets(cls, provider_data: dict[str, Any] | None = None) -> list[ColorTransformTarget]:
        """List the transform targets this provider can produce.

        Replaces an OCIO-flavoured display/view pair with one provider-opaque
        introspection surface: every provider has some enumerable set of "places I can
        transform pixels to" even if the shape differs completely (OCIO: display x view
        pairs; LUT-based: a flat list of LUT names; fixed-gamma: possibly one trivial
        entry, or none at all).

        Args:
            provider_data: Opaque, provider-specific detail that scopes or filters the
                returned targets (e.g. an OCIO config path, or a display name to scope
                views under it). Providers must tolerate unknown keys and a None value.

        Returns:
            The transform targets this provider currently exposes.
        """
        ...
