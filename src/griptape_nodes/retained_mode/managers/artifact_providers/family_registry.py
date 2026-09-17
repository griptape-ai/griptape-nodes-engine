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

    def get_decoders(self, family: type[ArtifactFamily], extension: str) -> list[type[BaseArtifactProvider]]:
        """Return every registered provider for ``extension`` that can decode ``family``.

        Args:
            family: The family whose decoder mixin providers must implement.
            extension: File extension without leading dot (e.g. "png").
        """
        return [
            provider_class
            for provider_class in self._registry.get_provider_classes_by_format(extension)
            if issubclass(provider_class, family.decoder_mixin)
        ]

    def resolve_decoder(
        self,
        family: type[ArtifactFamily],
        extension: str,
        preferred_friendly_name: str | None = None,
    ) -> type[BaseArtifactProvider] | None:
        """Resolve the decoder provider to use for ``extension`` within ``family``.

        Args:
            family: The family whose decoder mixin providers must implement.
            extension: File extension without leading dot (e.g. "png").
            preferred_friendly_name: If given, only a provider with this friendly
                name is returned; a name that matches no candidate returns
                ``None`` rather than silently falling back to another provider.

        Returns:
            The named provider, the first registered candidate, or ``None`` if
            there are no candidates or the named provider isn't among them.
        """
        candidates = self.get_decoders(family, extension)
        return self._resolve_from_candidates(candidates, preferred_friendly_name)

    def get_encoders(self, family: type[ArtifactFamily], output_format: str) -> list[type[BaseArtifactProvider]]:
        """Return every registered provider that can encode ``family`` to ``output_format``.

        Args:
            family: The family whose encoder mixin providers must implement.
            output_format: Desired output format without leading dot (e.g. "webp").
        """
        return [
            provider_class
            for provider_class in self._registry.get_all_provider_classes()
            if issubclass(provider_class, family.encoder_mixin)
            and output_format in provider_class.get_preview_formats()
        ]

    def resolve_encoder(
        self,
        family: type[ArtifactFamily],
        output_format: str,
        preferred_friendly_name: str | None = None,
    ) -> type[BaseArtifactProvider] | None:
        """Resolve the encoder provider to use for ``output_format`` within ``family``.

        Args:
            family: The family whose encoder mixin providers must implement.
            output_format: Desired output format without leading dot (e.g. "webp").
            preferred_friendly_name: If given, only a provider with this friendly
                name is returned; a name that matches no candidate returns
                ``None`` rather than silently falling back to another provider.

        Returns:
            The named provider, the first registered candidate, or ``None`` if
            there are no candidates or the named provider isn't among them.
        """
        candidates = self.get_encoders(family, output_format)
        return self._resolve_from_candidates(candidates, preferred_friendly_name)

    def resolve_conversion(self, family: type[ArtifactFamily], output_format: str) -> type[BaseArtifactProvider] | None:
        """Resolve the provider to convert ``family`` artifacts to ``output_format``.

        Args:
            family: The family whose encoder mixin providers must implement.
            output_format: Desired output format without leading dot (e.g. "webp").
        """
        return self.resolve_encoder(family, output_format)

    def supports_pipeline(self, family: type[ArtifactFamily], extension: str) -> bool:
        """True when ``extension`` has a decoder and at least one encoder exists for ``family``.

        Mirrors ``ArtifactManager.supports_displayable_image_pipeline``'s current
        shape: a decoder for this specific extension, plus any registered
        encoder for the family regardless of output format.

        Args:
            family: The family to check the decode/encode pipeline for.
            extension: File extension without leading dot (e.g. "png").
        """
        if self.resolve_decoder(family, extension) is None:
            return False
        return any(
            issubclass(provider_class, family.encoder_mixin)
            for provider_class in self._registry.get_all_provider_classes()
        )

    def _family_for_provider_class(self, provider_class: type[BaseArtifactProvider]) -> type[ArtifactFamily] | None:
        for family in self._families:
            if issubclass(provider_class, family.decoder_mixin) or issubclass(provider_class, family.encoder_mixin):
                return family
        return None

    @staticmethod
    def _resolve_from_candidates(
        candidates: list[type[BaseArtifactProvider]], preferred_friendly_name: str | None
    ) -> type[BaseArtifactProvider] | None:
        if preferred_friendly_name is not None:
            for candidate in candidates:
                if candidate.get_friendly_name().lower() == preferred_friendly_name.lower():
                    return candidate
            return None
        if not candidates:
            return None
        return candidates[0]
