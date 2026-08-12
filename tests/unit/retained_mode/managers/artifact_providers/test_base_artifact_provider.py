"""Tests for BaseArtifactProvider's preview-default contract.

A provider that does not generate previews advertises that by leaving
``get_preview_formats()`` empty. The other three preview-default helpers
(``get_default_preview_generator``, ``get_default_preview_format``,
``get_default_preview_generators``) raise NotImplementedError on the base
class so that callers who skip the ``get_preview_formats()`` gate fail
loudly instead of silently consuming an empty sentinel value.
"""

import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)


class _NonPreviewProvider(BaseArtifactProvider):
    """Concrete provider that opts out of preview generation."""

    @classmethod
    def get_friendly_name(cls) -> str:
        return "NonPreview"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"bin"}

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:  # noqa: ARG003
        return None


class TestBaseProviderPreviewContract:
    def test_get_preview_formats_default_is_empty(self) -> None:
        assert _NonPreviewProvider.get_preview_formats() == set()

    def test_get_default_preview_generator_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="does not generate previews"):
            _NonPreviewProvider.get_default_preview_generator()

    def test_get_default_preview_format_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="does not generate previews"):
            _NonPreviewProvider.get_default_preview_format()

    def test_get_default_preview_generators_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="does not generate previews"):
            _NonPreviewProvider.get_default_preview_generators()


class TestBaseProviderPermissionDefaults:
    """The permission hooks default to None so providers without a policy pay no cost.

    Concrete providers that gate reads/writes on media-specific properties
    (e.g. video codec) override these; every other provider inherits the
    allow-by-default behavior. Without these defaults the OSManager write path
    and the ``CheckArtifactReadPermissionRequest`` handler would need
    hasattr checks at every dispatch site.
    """

    def test_get_write_vetting_policy_default_is_none(self) -> None:
        # A ``None`` policy tells OSManager to skip vetting entirely for this
        # format -- neither staging nor a provider hook call happens. Every
        # provider that does not override this inherits the free path.
        assert _NonPreviewProvider.get_write_vetting_policy() is None

    def test_check_write_format_from_bytes_default_allows(self) -> None:
        # An instance is required because the hook is instance-level (providers
        # may hold lazy resources like ffprobe binaries). The base contract is
        # simply "return None to allow"; a fresh instance must honor that.
        provider = _NonPreviewProvider(registry=None)  # type: ignore[arg-type]
        assert provider.check_write_format_from_bytes(b"anything", "bin") is None

    def test_check_write_format_from_path_default_allows(self) -> None:
        provider = _NonPreviewProvider(registry=None)  # type: ignore[arg-type]
        assert provider.check_write_format_from_path("/some/path.bin", "bin") is None

    def test_check_read_permission_default_allows(self) -> None:
        provider = _NonPreviewProvider(registry=None)  # type: ignore[arg-type]
        assert provider.check_read_permission("/some/path.bin") is None
