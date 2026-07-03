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
