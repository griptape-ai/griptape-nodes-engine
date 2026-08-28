"""Tests for prepare_content_for_write on ImageArtifactProvider and ArtifactManager."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

_MODULE = "griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider"
COLLECT_PATH = f"{_MODULE}.collect_workflow_metadata"
IMAGE_OPEN_PATH = f"{_MODULE}.Image.open"
DRIVER_REGISTRY_PATH = f"{_MODULE}.ImageMetadataDriverRegistry.get_driver_for_format"


@pytest.fixture
def registry() -> ProviderRegistry:
    """Provide a bare ProviderRegistry."""
    return ProviderRegistry()


@pytest.fixture
def image_provider(registry: ProviderRegistry) -> ImageArtifactProvider:
    """Provide an ImageArtifactProvider backed by a bare registry."""
    return ImageArtifactProvider(registry=registry)


class TestImageArtifactProviderPrepareContentForWrite:
    """Tests for ImageArtifactProvider.prepare_content_for_write."""

    def test_delegates_to_injector_with_correct_args(self, image_provider: ImageArtifactProvider) -> None:
        """Test that prepare_content_for_write calls collect_workflow_metadata and injects via driver."""
        original_bytes = b"fake png bytes"
        injected_bytes = b"fake png bytes with metadata"
        metadata = {"gtn_saved_at": "2024-01-01T00:00:00+00:00"}

        mock_image = MagicMock()
        mock_image.format = "PNG"
        mock_driver = MagicMock()
        mock_driver.inject_metadata.return_value = injected_bytes

        with (
            patch(COLLECT_PATH, return_value=metadata),
            patch(IMAGE_OPEN_PATH, return_value=mock_image),
            patch(DRIVER_REGISTRY_PATH, return_value=mock_driver),
        ):
            result = image_provider.prepare_content_for_write(original_bytes, "image.png")

        assert result == injected_bytes
        mock_driver.inject_metadata.assert_called_once_with(mock_image, metadata)

    def test_returns_original_data_on_exception(
        self, image_provider: ImageArtifactProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that original data is returned and a warning is logged when injection raises."""
        original_bytes = b"fake png bytes"

        with patch(COLLECT_PATH, side_effect=RuntimeError("injection failed")), caplog.at_level(logging.WARNING):
            result = image_provider.prepare_content_for_write(original_bytes, "image.png")

        assert result == original_bytes
        assert any("Attempted to collect workflow metadata" in record.message for record in caplog.records)


class TestArtifactManagerPrepareContentForWrite:
    """Tests for ArtifactManager.prepare_content_for_write."""

    def test_returns_data_unchanged_for_unknown_extension(self, engine: Engine) -> None:
        """Test that data is returned unchanged when no provider handles the extension."""
        artifact_manager = engine.artifact_manager
        data = b"some binary data"

        result = artifact_manager.prepare_content_for_write(data, "archive.xyz_unknown")

        assert result is data

    def test_returns_data_unchanged_for_empty_extension(self, engine: Engine) -> None:
        """Test that data is returned unchanged when filename has no extension."""
        artifact_manager = engine.artifact_manager
        data = b"some binary data"

        result = artifact_manager.prepare_content_for_write(data, "no_extension_file")

        assert result is data

    def test_dispatches_to_provider_for_known_extension(self, engine: Engine) -> None:
        """Test that prepare_content_for_write dispatches to the image provider for .png files."""
        artifact_manager = engine.artifact_manager
        artifact_manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )
        original_bytes = b"fake png bytes"
        processed_bytes = b"fake png bytes with metadata"
        metadata = {"gtn_saved_at": "2024-01-01T00:00:00+00:00"}

        mock_image = MagicMock()
        mock_image.format = "PNG"
        mock_driver = MagicMock()
        mock_driver.inject_metadata.return_value = processed_bytes

        with (
            patch(COLLECT_PATH, return_value=metadata),
            patch(IMAGE_OPEN_PATH, return_value=mock_image),
            patch(DRIVER_REGISTRY_PATH, return_value=mock_driver),
        ):
            result = artifact_manager.prepare_content_for_write(original_bytes, "output.png")

        assert result == processed_bytes
        mock_driver.inject_metadata.assert_called_once_with(mock_image, metadata)
