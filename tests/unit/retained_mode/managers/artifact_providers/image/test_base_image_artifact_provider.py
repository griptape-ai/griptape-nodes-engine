"""Tests for shared image provider behaviour."""

import logging
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider import (
    BaseImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

_MODULE = "griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider"
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


class TestBaseImageArtifactProviderCollectMetadataForWrite:
    """Tests for BaseImageArtifactProvider.collect_metadata_for_write."""

    def test_collects_workflow_metadata_for_png(self, image_provider: ImageArtifactProvider) -> None:
        metadata = {"gtn_saved_at": "2024-01-01T00:00:00+00:00"}

        workflow = MagicMock()

        with patch(COLLECT_PATH, return_value=metadata) as collect:
            result = image_provider.collect_metadata_for_write("image.png", workflow)

        assert result == metadata
        collect.assert_called_once_with(image_provider.engine, workflow)

    def test_skips_collecting_for_formats_without_metadata(self, image_provider: ImageArtifactProvider) -> None:
        with patch(COLLECT_PATH) as mock_collect:
            result = image_provider.collect_metadata_for_write("image.jpg", None)

        mock_collect.assert_not_called()
        assert result == {}

    def test_returns_empty_and_warns_when_collecting_fails(
        self, image_provider: ImageArtifactProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(COLLECT_PATH, side_effect=RuntimeError("collect failed")), caplog.at_level(logging.WARNING):
            result = image_provider.collect_metadata_for_write("image.png", None)

        assert result == {}
        assert any("Attempted to collect workflow metadata" in record.message for record in caplog.records)


class TestBaseImageArtifactProviderPrepareContentForWrite:
    """Tests for BaseImageArtifactProvider.prepare_content_for_write."""

    def test_injects_the_given_metadata_via_driver(self, image_provider: ImageArtifactProvider) -> None:
        original_bytes = b"fake png bytes"
        injected_bytes = b"fake png bytes with metadata"
        metadata = {"gtn_saved_at": "2024-01-01T00:00:00+00:00"}

        mock_image = MagicMock()
        mock_image.format = "PNG"
        mock_image.__enter__.return_value = mock_image
        mock_driver = MagicMock()
        mock_driver.inject_metadata_into_bytes.return_value = injected_bytes

        with (
            patch(COLLECT_PATH) as mock_collect,
            patch(IMAGE_OPEN_PATH, return_value=mock_image),
            patch(DRIVER_REGISTRY_PATH, return_value=mock_driver),
        ):
            result = image_provider.prepare_content_for_write(original_bytes, "image.png", metadata)

        assert result == injected_bytes
        mock_driver.inject_metadata_into_bytes.assert_called_once_with(original_bytes, metadata)
        mock_image.__exit__.assert_called_once()
        # It runs on a worker thread, so it must not read engine state itself.
        mock_collect.assert_not_called()

    def test_returns_data_unchanged_without_metadata(self, image_provider: ImageArtifactProvider) -> None:
        with patch(IMAGE_OPEN_PATH) as mock_open:
            result = image_provider.prepare_content_for_write(b"fake png bytes", "image.png", {})

        assert result == b"fake png bytes"
        mock_open.assert_not_called()

    def test_returns_empty_data_unchanged(self, image_provider: ImageArtifactProvider) -> None:
        result = image_provider.prepare_content_for_write(b"", "image.png", {"gtn_saved_at": "x"})

        assert result == b""


def _pil_bytes(fmt: str, mode: str = "RGB") -> bytes:
    buf = BytesIO()
    Image.new(mode, (1, 1), color=0 if mode == "P" else "white").save(buf, format=fmt)
    return buf.getvalue()


def _png_bytes() -> bytes:
    return _pil_bytes("PNG")


def _jpeg_bytes() -> bytes:
    return _pil_bytes("JPEG")


def _gif_bytes() -> bytes:
    return _pil_bytes("GIF", mode="P")


def _ico_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (16, 16)).save(buf, format="ICO")
    return buf.getvalue()


class TestImageDetectFormat:
    @pytest.mark.parametrize(
        ("byte_sample", "expected_format"),
        [
            pytest.param(_png_bytes(), "png", id="png"),
            pytest.param(_jpeg_bytes(), "jpg", id="jpeg"),
            pytest.param(_gif_bytes(), "gif", id="gif"),
            pytest.param(_pil_bytes("WEBP"), "webp", id="webp"),
            pytest.param(_pil_bytes("BMP"), "bmp", id="bmp"),
            pytest.param(_pil_bytes("TIFF"), "tiff", id="tiff"),
        ],
    )
    def test_detects_supported_format(self, byte_sample: bytes, expected_format: str) -> None:
        assert BaseImageArtifactProvider.detect_format(byte_sample) == expected_format

    @pytest.mark.parametrize(
        "byte_sample",
        [
            pytest.param(_ico_bytes(), id="ico"),
            pytest.param(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16, id="heic"),
            pytest.param(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16, id="heif-mif1"),
            pytest.param(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16, id="avif"),
        ],
    )
    def test_unsupported_format_is_not_claimed(self, byte_sample: bytes) -> None:
        """Format detection declines these undeclared formats (GH#5614)."""
        assert BaseImageArtifactProvider.detect_format(byte_sample) is None

    def test_riff_without_webp_marker_returns_none(self) -> None:
        """A RIFF header alone (e.g. WAV / AVI) must not be claimed as WebP."""
        assert BaseImageArtifactProvider.detect_format(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16) is None

    def test_unidentifiable_returns_none(self) -> None:
        assert BaseImageArtifactProvider.detect_format(b"not an image") is None

    def test_short_data_returns_none(self) -> None:
        assert BaseImageArtifactProvider.detect_format(b"\x89PNG") is None


class TestDetectFormatContract:
    """Every *sampled* return value of detect_format() must be a declared format.

    This does not catch a new sniffing branch that returns an undeclared format
    with no sample here to exercise it - that shape of drift (GH#5614's heic /
    avif / ico sniffing) is guarded by test_unsupported_format_is_not_claimed.
    """

    @pytest.mark.parametrize(
        "byte_sample",
        [
            _png_bytes(),
            _jpeg_bytes(),
            _gif_bytes(),
            _pil_bytes("WEBP"),
            _pil_bytes("BMP"),
            _pil_bytes("TIFF"),
        ],
        ids=["png", "jpeg", "gif", "webp", "bmp", "tiff"],
    )
    def test_detect_format_return_values_are_all_supported(self, byte_sample: bytes) -> None:
        detected = BaseImageArtifactProvider.detect_format(byte_sample)
        assert detected is not None
        assert detected in ImageArtifactProvider.get_supported_formats()
