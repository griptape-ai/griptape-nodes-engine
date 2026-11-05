"""Tests for PILRoundedPreviewGenerator."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from griptape_nodes.image import GriptapeImage
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_rounded_preview_generator import (
    PILRoundedPreviewGenerator,
)
from tests.unit.image.sample_images import BLUE, RED, encode, solid


@pytest.fixture
def temp_test_image() -> Generator[str, None, None]:
    """Create a temporary test image."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Create a simple red 200x100 image
        f.write(encode(solid(200, 100, RED), "png"))
        temp_path = f.name

    yield temp_path

    # Cleanup
    temp_file = Path(temp_path)
    if temp_file.exists():
        temp_file.unlink()


@pytest.fixture
def temp_transparent_image() -> Generator[str, None, None]:
    """Create a temporary test image with transparency."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Create RGBA image with semi-transparent red
        f.write(encode(solid(200, 100, (*RED, 128)), "png"))
        temp_path = f.name

    yield temp_path

    # Cleanup
    temp_file = Path(temp_path)
    if temp_file.exists():
        temp_file.unlink()


@pytest.fixture
def temp_output_dir() -> Generator[str, None, None]:
    """Create temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestPILRoundedPreviewGeneratorParameters:
    """Test parameter validation."""

    def test_invalid_max_width_negative(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that negative max_width raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": -100, "max_height": 100, "corner_radius_percent": 2.0},
            )

    def test_invalid_max_width_zero(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that zero max_width raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 0, "max_height": 100, "corner_radius_percent": 2.0},
            )

    def test_invalid_max_width_too_large(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that max_width > 8192 raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 8193, "max_height": 100, "corner_radius_percent": 2.0},
            )

    def test_max_width_string_coercion(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that string max_width is coerced to int (Pydantic feature)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": "100", "max_height": 100, "corner_radius_percent": 2.0},
        )
        # Pydantic should coerce string "100" to int 100
        assert generator.params.max_width == 100  # noqa: PLR2004

    def test_invalid_max_height_negative(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that negative max_height raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": -100, "corner_radius_percent": 2.0},
            )

    def test_invalid_max_height_zero(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that zero max_height raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": 0, "corner_radius_percent": 2.0},
            )

    def test_invalid_max_height_too_large(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that max_height > 8192 raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": 8193, "corner_radius_percent": 2.0},
            )

    def test_invalid_corner_radius_percent_negative(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that negative corner_radius_percent raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": 100, "corner_radius_percent": -1.0},
            )

    def test_invalid_corner_radius_percent_too_large(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that corner_radius_percent > 10 raises ValidationError."""
        with pytest.raises(ValidationError):
            PILRoundedPreviewGenerator(
                source_file_location=temp_test_image,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": 100, "corner_radius_percent": 11.0},
            )

    def test_corner_radius_percent_string_coercion(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that string corner_radius_percent is coerced to float (Pydantic feature)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 100, "max_height": 100, "corner_radius_percent": "2.0"},
        )
        # Pydantic should coerce string "2.0" to float 2.0
        assert generator.params.corner_radius_percent == 2.0  # noqa: PLR2004

    def test_valid_parameters(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that valid parameters pass validation."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        assert generator.params.max_width == 150  # noqa: PLR2004
        assert generator.params.max_height == 150  # noqa: PLR2004
        assert generator.params.corner_radius_percent == 2.0  # noqa: PLR2004


class TestPILRoundedPreviewGeneratorClassMethods:
    """Test class methods."""

    def test_get_friendly_name(self) -> None:
        """Test get_friendly_name returns correct name."""
        assert PILRoundedPreviewGenerator.get_friendly_name() == "Rounded Image Preview Generation"

    def test_get_supported_source_formats(self) -> None:
        """Test get_supported_source_formats returns correct set."""
        formats = PILRoundedPreviewGenerator.get_supported_source_formats()
        assert isinstance(formats, set)
        assert "png" in formats
        assert "jpg" in formats
        assert "jpeg" in formats
        assert "webp" in formats

    def test_get_supported_preview_formats(self) -> None:
        """Test get_supported_preview_formats returns correct set."""
        formats = PILRoundedPreviewGenerator.get_supported_preview_formats()
        assert isinstance(formats, set)
        assert "png" in formats
        assert "jpg" in formats
        assert "webp" in formats

    def test_get_parameters(self) -> None:
        """Test get_parameters returns correct Pydantic model class."""
        params_model_class = PILRoundedPreviewGenerator.get_parameters()
        model_fields = params_model_class.model_fields

        assert len(model_fields) == 3  # noqa: PLR2004
        assert "max_width" in model_fields
        assert "max_height" in model_fields
        assert "corner_radius_percent" in model_fields

        # Verify defaults
        assert model_fields["max_width"].default == 1024  # noqa: PLR2004
        assert model_fields["max_height"].default == 1024  # noqa: PLR2004
        assert model_fields["corner_radius_percent"].default == 2.0  # noqa: PLR2004


class TestPILRoundedPreviewGeneratorGeneration:
    """Test preview generation."""

    @pytest.mark.asyncio
    async def test_generate_basic_rounded_corners(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating preview with basic rounded corners."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        result_filename = await generator.attempt_generate_preview()

        assert result_filename == "output.png"
        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

        # Verify image properties
        with GriptapeImage.open(output_path) as img:
            assert img.color_space == "RGBA"
            # Image should be resized to fit 150x150
            assert img.width <= 150  # noqa: PLR2004
            assert img.height <= 150  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_generate_no_rounding(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating preview with corner_radius_percent = 0 (no rounding)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 0.0},
        )

        result_filename = await generator.attempt_generate_preview()

        assert result_filename == "output.png"
        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

    @pytest.mark.asyncio
    async def test_generate_large_corner_radius_percent(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating preview with maximum corner_radius_percent (should clamp to half dimension)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 100, "max_height": 100, "corner_radius_percent": 10.0},
        )

        result_filename = await generator.attempt_generate_preview()

        assert result_filename == "output.png"
        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

    @pytest.mark.asyncio
    async def test_generate_transparent_png_input(self, temp_transparent_image: str, temp_output_dir: str) -> None:
        """Test generating preview from transparent PNG input."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_transparent_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        result_filename = await generator.attempt_generate_preview()

        assert result_filename == "output.png"
        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

        # Verify output has transparency
        with GriptapeImage.open(output_path) as img:
            assert img.color_space == "RGBA"

    @pytest.mark.asyncio
    async def test_generate_png_output(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating PNG output (keeps transparency)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        with GriptapeImage.open(output_path) as img:
            assert img.color_space == "RGBA"
            assert img.format == "PNG"

    @pytest.mark.asyncio
    async def test_generate_webp_output(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating WEBP output (keeps transparency)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="webp",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.webp",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        with GriptapeImage.open(output_path) as img:
            # WEBP can be RGBA
            assert img.color_space in ("RGBA", "RGB")
            assert img.format == "WEBP"

    @pytest.mark.asyncio
    async def test_generate_jpeg_output(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test generating JPEG output (composites on white background)."""
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="jpg",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.jpg",
            params={"max_width": 150, "max_height": 150, "corner_radius_percent": 2.0},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        with GriptapeImage.open(output_path) as img:
            # JPEG should be RGB (no alpha)
            assert img.color_space == "RGB"
            assert img.format == "JPEG"

    @pytest.mark.asyncio
    async def test_aspect_ratio_preserved(self, temp_test_image: str, temp_output_dir: str) -> None:
        """Test that aspect ratio is preserved during resize."""
        # Original is 200x100 (2:1 aspect ratio)
        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_test_image,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 100, "max_height": 100, "corner_radius_percent": 1.0},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        with GriptapeImage.open(output_path) as img:
            # Should be scaled down to 100x50 to fit in 100x100 while preserving 2:1 ratio
            assert img.width == 100  # noqa: PLR2004
            assert img.height == 50  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_small_image(self, temp_output_dir: str) -> None:
        """Test with very small image."""
        # Create tiny 10x10 image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(encode(solid(10, 10, BLUE), "png"))
            tiny_path = f.name

        try:
            generator = PILRoundedPreviewGenerator(
                source_file_location=tiny_path,
                preview_format="png",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.png",
                params={"max_width": 100, "max_height": 100, "corner_radius_percent": 0.5},
            )

            result_filename = await generator.attempt_generate_preview()

            output_path = Path(temp_output_dir) / result_filename
            assert output_path.exists()

            with GriptapeImage.open(output_path) as img:
                # Small image should remain 10x10
                assert img.width == 10  # noqa: PLR2004
                assert img.height == 10  # noqa: PLR2004
        finally:
            tiny_file = anyio.Path(tiny_path)
            if await tiny_file.exists():
                await tiny_file.unlink()
