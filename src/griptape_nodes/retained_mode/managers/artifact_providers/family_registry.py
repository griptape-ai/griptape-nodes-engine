"""Registry that resolves artifact families and, per family, their provider pipelines."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.artifact_providers.artifact_family import ImageFamily

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.artifact_family import ArtifactFamily
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
        BaseArtifactProvider,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry


class FamilyRegistry:
    """Resolves which ``ArtifactFamily`` a format/provider belongs to.

    Families themselves are hardcoded here (only ``ImageFamily`` exists today),
    mirroring how ``ArtifactManager`` hardcodes its default provider list at
    boot - not pluggable, per the design's "no ``RegisterArtifactFamilyRequest``
    yet" decision.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        """Initialize with the ``ProviderRegistry`` whose providers this resolves families for."""
        self._registry = registry
        self._families: list[type[ArtifactFamily]] = [ImageFamily]

    def get_family_for_extension(self, extension: str) -> type[ArtifactFamily] | None:
        """Return the family a registered provider for ``extension`` belongs to, if any.

        Args:
            extension: File extension without leading dot (e.g. "png").

        Returns:
            The matching family, or ``None`` if no registered provider for this
            extension implements any known family's decoder/encoder mixin.
        """
        for provider_class in self._registry.get_provider_classes_by_format(extension):
            family = self._family_for_provider_class(provider_class)
            if family is not None:
                return family
        return None

    def resolve_family(self, extension: str, data: bytes | None = None) -> type[ArtifactFamily] | None:
        """Resolve the family for a file, preferring content sniffing over the extension.

        Args:
            extension: File extension without leading dot, used as a fallback.
            data: Raw file bytes to sniff, or ``None`` to skip sniffing.

        Returns:
            The family of the first provider that positively claims ``data``, else
            the family resolved from ``extension``, else ``None``.
        """
        if data is not None:
            for provider_class in self._registry.get_all_provider_classes():
                sniffed_extension = provider_class.detect_format(data)
                if sniffed_extension is None:
                    continue
                family = self._family_for_provider_class(provider_class)
                if family is not None:
                    return family
        return self.get_family_for_extension(extension)

    def _family_for_provider_class(self, provider_class: type[BaseArtifactProvider]) -> type[ArtifactFamily] | None:
        for family in self._families:
            if issubclass(provider_class, family.decoder_mixin) or issubclass(provider_class, family.encoder_mixin):
                return family
        return None
