"""Tests for ImageArtifactProvider.encode()."""

from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
    DecodedImageArtifact,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import ImageArtifactSituation
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry


def _make_decoded(pixel_data: np.ndarray, *, channel_layout: str = "RGB") -> DecodedImageArtifact:
    return DecodedImageArtifact(
        pixel_data=pixel_data,
        source_color_space="sRGB",
        bit_depth=8,
        channel_layout=channel_layout,
    )


@pytest.fixture
def registry() -> ProviderRegistry:
    """Provide a bare ProviderRegistry."""
    return ProviderRegistry()


@pytest.fixture
def image_provider(registry: ProviderRegistry) -> ImageArtifactProvider:
    """Provide an ImageArtifactProvider backed by a bare registry."""
    return ImageArtifactProvider(registry=registry)


class TestImageArtifactProviderEncode:
    """Tests for ImageArtifactProvider.encode()."""

    def test_encode_uint8_rgb_viewer_roundtrips_via_pil(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.full((3, 4, 3), 128, dtype=np.uint8)
        decoded = _make_decoded(pixel_data)

        encoded = image_provider.encode(decoded, ImageArtifactSituation.VIEWER)

        with Image.open(BytesIO(encoded)) as img:
            assert img.format == "WEBP"
            assert img.size == (4, 3)
            assert img.mode == "RGB"

    def test_encode_grayscale_uint8_viewer(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.full((3, 4), 200, dtype=np.uint8)
        decoded = _make_decoded(pixel_data, channel_layout="Grayscale")

        encoded = image_provider.encode(decoded, ImageArtifactSituation.VIEWER)

        with Image.open(BytesIO(encoded)) as img:
            assert img.format == "WEBP"
            assert img.size == (4, 3)

    def test_encode_int32_i_mode_normalizes_to_uint8(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.array([[1000, 60000], [30000, 45000]], dtype=np.int32)
        decoded = _make_decoded(pixel_data, channel_layout="Grayscale")

        encoded = image_provider.encode(decoded, ImageArtifactSituation.VIEWER)

        with Image.open(BytesIO(encoded)) as img:
            reopened = np.asarray(img)
            assert reopened.min() <= 5  # noqa: PLR2004
            assert reopened.max() >= 250  # noqa: PLR2004

    def test_encode_float32_f_mode_normalizes_to_uint8(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.array([[-40.0, 300.0], [50.0, 100.0]], dtype=np.float32)
        decoded = _make_decoded(pixel_data, channel_layout="Grayscale")

        encoded = image_provider.encode(decoded, ImageArtifactSituation.VIEWER)

        with Image.open(BytesIO(encoded)) as img:
            reopened = np.asarray(img)
            assert not np.isnan(reopened).any()
            assert reopened.min() <= 5  # noqa: PLR2004
            assert reopened.max() >= 250  # noqa: PLR2004

    def test_encode_uint8_passthrough_does_not_rescale(self) -> None:
        pixel_data = np.array([[50, 200], [100, 150]], dtype=np.uint8)
        normalized = ImageArtifactProvider._normalize_to_uint8(pixel_data)

        assert normalized is pixel_data
        assert normalized.min() == 50  # noqa: PLR2004
        assert normalized.max() == 200  # noqa: PLR2004

    def test_encode_constant_value_image_does_not_divide_by_zero(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.zeros((2, 2), dtype=np.float32)
        decoded = _make_decoded(pixel_data, channel_layout="Grayscale")

        encoded = image_provider.encode(decoded, ImageArtifactSituation.VIEWER)

        with Image.open(BytesIO(encoded)) as img:
            reopened = np.asarray(img)
            assert not np.isnan(reopened).any()
            assert reopened.max() == 0

    def test_encode_thumbnail_situation_uses_default_format(self, image_provider: ImageArtifactProvider) -> None:
        pixel_data = np.full((3, 4, 3), 128, dtype=np.uint8)
        decoded = _make_decoded(pixel_data)

        encoded = image_provider.encode(decoded, ImageArtifactSituation.THUMBNAIL)

        with Image.open(BytesIO(encoded)) as img:
            assert img.format == "WEBP"
