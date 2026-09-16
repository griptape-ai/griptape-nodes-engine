"""Tests for ImageArtifactProvider.decode()."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PIL import Image

from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import ImageArtifactSituation
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

if TYPE_CHECKING:
    from pathlib import Path

_STANDARD_BIT_DEPTH = 8


def _write_image(tmp_path: Path, name: str, mode: str, size: tuple[int, int], fmt: str) -> str:
    path = tmp_path / name
    Image.new(mode, size, color=0 if mode == "P" else "white").save(path, format=fmt)
    return str(path)


@pytest.fixture
def registry() -> ProviderRegistry:
    """Provide a bare ProviderRegistry."""
    return ProviderRegistry()


@pytest.fixture
def image_provider(registry: ProviderRegistry) -> ImageArtifactProvider:
    """Provide an ImageArtifactProvider backed by a bare registry."""
    return ImageArtifactProvider(registry=registry)


class TestImageArtifactProviderDecode:
    """Tests for ImageArtifactProvider.decode()."""

    def test_png_rgb_viewer(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "rgb.png", "RGB", (4, 3), "PNG")

        decoded = image_provider.decode(path, ImageArtifactSituation.VIEWER)

        assert decoded.pixel_data.shape == (3, 4, 3)
        assert decoded.source_color_space == "sRGB"
        assert decoded.bit_depth == _STANDARD_BIT_DEPTH
        assert decoded.channel_layout == "RGB"

    def test_jpeg_rgb_viewer(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "rgb.jpg", "RGB", (4, 3), "JPEG")

        decoded = image_provider.decode(path, ImageArtifactSituation.VIEWER)

        assert decoded.pixel_data.shape == (3, 4, 3)
        assert decoded.source_color_space == "sRGB"
        assert decoded.bit_depth == _STANDARD_BIT_DEPTH
        assert decoded.channel_layout == "RGB"

    def test_png_rgb_thumbnail_downsamples(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "large.png", "RGB", (2000, 1000), "PNG")

        decoded = image_provider.decode(path, ImageArtifactSituation.THUMBNAIL)

        height, width = decoded.pixel_data.shape[0], decoded.pixel_data.shape[1]
        max_width = ImageArtifactProvider.get_thumbnail_max_size()[0]
        max_height = ImageArtifactProvider.get_thumbnail_max_size()[1]
        assert width <= max_width
        assert height <= max_height
        assert width > height  # aspect ratio preserved (2:1 source)

    def test_palette_mode_resolves_to_rgb(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "palette.png", "P", (4, 3), "PNG")

        decoded = image_provider.decode(path, ImageArtifactSituation.VIEWER)

        assert decoded.channel_layout in ("RGB", "RGBA")
        assert decoded.source_color_space == "sRGB"
        assert decoded.bit_depth == _STANDARD_BIT_DEPTH

    def test_grayscale_viewer(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "gray.png", "L", (4, 3), "PNG")

        decoded = image_provider.decode(path, ImageArtifactSituation.VIEWER)

        assert decoded.pixel_data.shape == (3, 4)
        assert decoded.channel_layout == "Grayscale"
        assert decoded.source_color_space == "Grayscale"
        assert decoded.bit_depth == _STANDARD_BIT_DEPTH

    def test_grayscale_thumbnail_downsamples(self, image_provider: ImageArtifactProvider, tmp_path: Path) -> None:
        path = _write_image(tmp_path, "large_gray.png", "L", (2000, 1000), "PNG")

        decoded = image_provider.decode(path, ImageArtifactSituation.THUMBNAIL)

        height, width = decoded.pixel_data.shape[0], decoded.pixel_data.shape[1]
        max_width, max_height = ImageArtifactProvider.get_thumbnail_max_size()
        assert width <= max_width
        assert height <= max_height
        assert decoded.channel_layout == "Grayscale"
        assert decoded.source_color_space == "Grayscale"
        assert decoded.bit_depth == _STANDARD_BIT_DEPTH
