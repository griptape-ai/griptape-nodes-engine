"""Tests for FamilyRegistry's format-identification step (GH#4741 parent).

Constructs FamilyRegistry directly with a real ProviderRegistry that has
ImageArtifactProvider registered - no ArtifactManager wiring involved yet.
"""

from griptape_nodes.retained_mode.managers.artifact_providers.artifact_family import ImageFamily
from griptape_nodes.retained_mode.managers.artifact_providers.family_registry import FamilyRegistry
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

_PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _build_registry_with_image_provider() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register_provider(ImageArtifactProvider)
    return registry


class TestGetFamilyForExtension:
    def test_get_family_for_extension_returns_image_family_for_png(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        assert family_registry.get_family_for_extension("png") is ImageFamily

    def test_get_family_for_extension_returns_none_for_unregistered_extension(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        assert family_registry.get_family_for_extension("mp4") is None


class TestResolveFamily:
    def test_resolve_family_prefers_content_sniff_over_extension_when_data_given(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        result = family_registry.resolve_family("txt", data=_PNG_MAGIC_BYTES)

        assert result is ImageFamily

    def test_resolve_family_falls_back_to_extension_when_data_is_none(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        result = family_registry.resolve_family("png", data=None)

        assert result is ImageFamily

    def test_resolve_family_falls_back_to_extension_when_no_provider_positively_claims_bytes(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        result = family_registry.resolve_family("png", data=b"not a recognizable header")

        assert result is ImageFamily

    def test_resolve_family_returns_none_when_nothing_matches(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        result = family_registry.resolve_family("mp4", data=b"not a recognizable header")

        assert result is None
