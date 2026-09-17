"""Tests for FamilyRegistry's format-identification step (GH#4741 parent).

Constructs FamilyRegistry directly with a real ProviderRegistry that has
ImageArtifactProvider registered - no ArtifactManager wiring involved yet.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import ClassVar

import numpy as np

from griptape_nodes.retained_mode.managers.artifact_providers.artifact_family import ImageFamily
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import BaseArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.family_registry import FamilyRegistry
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
    DecodedImageArtifact,
    ImageArtifactDecoderMixin,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_encoder_mixin import ImageArtifactEncoderMixin
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import ImageArtifactSituation
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


class _AlternateImageProvider(BaseArtifactProvider, ImageArtifactDecoderMixin, ImageArtifactEncoderMixin):
    """A second Image-family provider, so registration order/override behavior can be tested."""

    @classmethod
    def get_friendly_name(cls) -> str:
        return "AlternateImage"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"png", "jpg"}

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"webp"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Default"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "webp"

    @classmethod
    def get_default_preview_generators(cls) -> list:
        return []

    @classmethod
    def get_artifact_metadata(cls, _source_path: str) -> None:
        return None

    def decode(self, source_path: str, situation: ImageArtifactSituation) -> DecodedImageArtifact:  # noqa: ARG002
        return DecodedImageArtifact(
            pixel_data=np.zeros((1, 1, 3)), source_color_space="sRGB", bit_depth=8, channel_layout="RGB"
        )

    def encode(
        self,
        decoded_artifact: DecodedImageArtifact,  # noqa: ARG002
        situation: ImageArtifactSituation,  # noqa: ARG002
        format: str | None = None,  # noqa: A002, ARG002
    ) -> bytes:
        return b""


def _build_registry_with_two_image_providers() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register_provider(ImageArtifactProvider)
    registry.register_provider(_AlternateImageProvider)
    return registry


class TestGetDecoders:
    def test_get_decoders_returns_decoder_providers_for_extension(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        decoders = family_registry.get_decoders(ImageFamily, "png")

        assert set(decoders) == {ImageArtifactProvider, _AlternateImageProvider}

    def test_get_decoders_returns_empty_list_for_unregistered_extension(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        assert family_registry.get_decoders(ImageFamily, "mp4") == []


class TestResolveDecoder:
    def test_resolve_decoder_returns_first_registered_when_no_override_given(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        decoder = family_registry.resolve_decoder(ImageFamily, "png")

        assert decoder is ImageArtifactProvider

    def test_resolve_decoder_honors_explicit_override(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        decoder = family_registry.resolve_decoder(ImageFamily, "png", preferred_friendly_name="AlternateImage")

        assert decoder is _AlternateImageProvider

    def test_resolve_decoder_returns_none_when_override_names_unregistered_provider(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        decoder = family_registry.resolve_decoder(ImageFamily, "png", preferred_friendly_name="NoSuchProvider")

        assert decoder is None

    def test_resolve_decoder_returns_none_when_no_decoders_registered(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_image_provider())

        assert family_registry.resolve_decoder(ImageFamily, "mp4") is None


class TestGetEncoders:
    def test_get_encoders_returns_multiple_providers_for_same_output_format(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        encoders = family_registry.get_encoders(ImageFamily, "webp")

        assert set(encoders) == {ImageArtifactProvider, _AlternateImageProvider}

    def test_get_encoders_returns_empty_list_for_unsupported_format(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        assert family_registry.get_encoders(ImageFamily, "not-a-format") == []

    def test_get_encoders_with_no_format_returns_every_encoder_for_family(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        encoders = family_registry.get_encoders(ImageFamily)

        assert set(encoders) == {ImageArtifactProvider, _AlternateImageProvider}


class TestResolveEncoder:
    def test_resolve_encoder_honors_explicit_override(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        encoder = family_registry.resolve_encoder(ImageFamily, "webp", preferred_friendly_name="AlternateImage")

        assert encoder is _AlternateImageProvider

    def test_resolve_encoder_returns_none_when_override_names_unregistered_provider(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        encoder = family_registry.resolve_encoder(ImageFamily, "webp", preferred_friendly_name="NoSuchProvider")

        assert encoder is None

    def test_resolve_encoder_with_no_format_returns_first_registered(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        assert family_registry.resolve_encoder(ImageFamily) is ImageArtifactProvider


class TestResolveConversion:
    def test_resolve_conversion_delegates_to_resolve_encoder(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        assert family_registry.resolve_conversion(ImageFamily, "webp") == family_registry.resolve_encoder(
            ImageFamily, "webp"
        )


class TestSupportsPipeline:
    def test_supports_pipeline_true_when_decoder_and_encoder_exist(self) -> None:
        family_registry = FamilyRegistry(_build_registry_with_two_image_providers())

        assert family_registry.supports_pipeline(ImageFamily, "png") is True

    def test_supports_pipeline_false_when_only_decoder_exists(self) -> None:
        registry = ProviderRegistry()

        class _DecodeOnlyProvider(BaseArtifactProvider, ImageArtifactDecoderMixin):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "DecodeOnly"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return set()

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

            @classmethod
            def get_artifact_metadata(cls, _source_path: str) -> None:
                return None

            def decode(self, source_path: str, situation: ImageArtifactSituation) -> DecodedImageArtifact:  # noqa: ARG002
                return DecodedImageArtifact(
                    pixel_data=np.zeros((1, 1, 3)), source_color_space="sRGB", bit_depth=8, channel_layout="RGB"
                )

        registry.register_provider(_DecodeOnlyProvider)
        family_registry = FamilyRegistry(registry)

        assert family_registry.supports_pipeline(ImageFamily, "png") is False


# Step 4: a second, unrelated family - proves FamilyRegistry has no Image-specific
# assumptions baked in (hardcoded "Image" string, hardcoded situation type, etc.).
# Test-only: FixtureFamily/FixtureProvider never appear outside this file.


class _FixtureSituation(StrEnum):
    RAW = "raw"
    COOKED = "cooked"


class _FixtureDecoderMixin(ABC):
    @abstractmethod
    def fixture_decode(self, source_path: str, situation: _FixtureSituation) -> str: ...


class _FixtureEncoderMixin(ABC):
    @abstractmethod
    def fixture_encode(self, decoded: str, situation: _FixtureSituation) -> bytes: ...


class FixtureFamily:
    family_id: ClassVar[str] = "fixturefamily"
    situation_type: ClassVar[type[StrEnum]] = _FixtureSituation
    situation_fallbacks: ClassVar[dict] = {
        _FixtureSituation.RAW: None,
        _FixtureSituation.COOKED: _FixtureSituation.RAW,
    }
    decoder_mixin: ClassVar[type[ABC]] = _FixtureDecoderMixin
    encoder_mixin: ClassVar[type[ABC]] = _FixtureEncoderMixin


class FixtureProvider(BaseArtifactProvider, _FixtureDecoderMixin, _FixtureEncoderMixin):
    """Registered for a fake extension that cannot collide with any real format."""

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Fixture"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"fixturefmt"}

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"fixturepreview"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Default"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "fixturepreview"

    @classmethod
    def get_default_preview_generators(cls) -> list:
        return []

    @classmethod
    def get_artifact_metadata(cls, _source_path: str) -> None:
        return None

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:
        if data.startswith(b"FIXTURE_MAGIC"):
            return "fixturefmt"
        return None

    def fixture_decode(self, source_path: str, situation: _FixtureSituation) -> str:  # noqa: ARG002
        return "decoded"

    def fixture_encode(self, decoded: str, situation: _FixtureSituation) -> bytes:  # noqa: ARG002
        return b"encoded"


class _AlternateFixtureProvider(BaseArtifactProvider, _FixtureDecoderMixin, _FixtureEncoderMixin):
    """A second Fixture-family provider, mirroring _AlternateImageProvider's role."""

    @classmethod
    def get_friendly_name(cls) -> str:
        return "AlternateFixture"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"fixturefmt"}

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"fixturepreview"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Default"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "fixturepreview"

    @classmethod
    def get_default_preview_generators(cls) -> list:
        return []

    @classmethod
    def get_artifact_metadata(cls, _source_path: str) -> None:
        return None

    def fixture_decode(self, source_path: str, situation: _FixtureSituation) -> str:  # noqa: ARG002
        return "decoded"

    def fixture_encode(self, decoded: str, situation: _FixtureSituation) -> bytes:  # noqa: ARG002
        return b"encoded"


def _build_fixture_family_registry() -> FamilyRegistry:
    registry = ProviderRegistry()
    registry.register_provider(FixtureProvider)
    registry.register_provider(_AlternateFixtureProvider)
    return FamilyRegistry(registry, families=[ImageFamily, FixtureFamily])


class TestFamilyRegistryIsFamilyAgnostic:
    """Re-runs the Step 2/3 matrix against FixtureFamily instead of ImageFamily."""

    def test_get_family_for_extension_returns_fixture_family(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.get_family_for_extension("fixturefmt") is FixtureFamily

    def test_resolve_family_prefers_content_sniff_over_extension(self) -> None:
        family_registry = _build_fixture_family_registry()

        result = family_registry.resolve_family("txt", data=b"FIXTURE_MAGIC_HEADER")

        assert result is FixtureFamily

    def test_resolve_family_falls_back_to_extension(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.resolve_family("fixturefmt", data=None) is FixtureFamily

    def test_get_decoders_returns_both_fixture_providers(self) -> None:
        family_registry = _build_fixture_family_registry()

        decoders = family_registry.get_decoders(FixtureFamily, "fixturefmt")

        assert set(decoders) == {FixtureProvider, _AlternateFixtureProvider}

    def test_resolve_decoder_returns_first_registered_when_no_override_given(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.resolve_decoder(FixtureFamily, "fixturefmt") is FixtureProvider

    def test_resolve_decoder_honors_explicit_override(self) -> None:
        family_registry = _build_fixture_family_registry()

        decoder = family_registry.resolve_decoder(
            FixtureFamily, "fixturefmt", preferred_friendly_name="AlternateFixture"
        )

        assert decoder is _AlternateFixtureProvider

    def test_get_encoders_returns_both_fixture_providers(self) -> None:
        family_registry = _build_fixture_family_registry()

        encoders = family_registry.get_encoders(FixtureFamily, "fixturepreview")

        assert set(encoders) == {FixtureProvider, _AlternateFixtureProvider}

    def test_resolve_encoder_honors_explicit_override(self) -> None:
        family_registry = _build_fixture_family_registry()

        encoder = family_registry.resolve_encoder(
            FixtureFamily, "fixturepreview", preferred_friendly_name="AlternateFixture"
        )

        assert encoder is _AlternateFixtureProvider

    def test_resolve_conversion_delegates_to_resolve_encoder(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.resolve_conversion(FixtureFamily, "fixturepreview") == family_registry.resolve_encoder(
            FixtureFamily, "fixturepreview"
        )

    def test_supports_pipeline_true_when_decoder_and_encoder_exist(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.supports_pipeline(FixtureFamily, "fixturefmt") is True

    def test_image_and_fixture_families_do_not_cross_resolve(self) -> None:
        family_registry = _build_fixture_family_registry()

        assert family_registry.get_decoders(ImageFamily, "fixturefmt") == []
        assert family_registry.get_decoders(FixtureFamily, "png") == []
