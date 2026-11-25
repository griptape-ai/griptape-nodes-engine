"""Tests for PILRoundedPreviewGenerator."""

from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_rounded_preview_generator import (
    PILRoundedPreviewGenerator,
)


@pytest.fixture
def temp_test_image(tmp_path: Path) -> str:
    """Create a temporary test image."""
    temp_path = str(tmp_path / "temp_test_image.png")
    # Create a simple red 200x100 image
    img = Image.new("RGB", (200, 100), color="red")
    img.save(temp_path, format="PNG")

    return temp_path


@pytest.fixture
def temp_transparent_image(tmp_path: Path) -> str:
    """Create a temporary test image with transparency."""
    temp_path = str(tmp_path / "temp_transparent_image.png")
    # Create RGBA image with semi-transparent red
    img = Image.new("RGBA", (200, 100), color=(255, 0, 0, 128))
    img.save(temp_path, format="PNG")

    return temp_path


@pytest.fixture
def temp_output_dir(tmp_path: Path) -> str:
    """Create temporary output directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return str(output_dir)


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
        with Image.open(output_path) as img:
            assert img.mode == "RGBA"
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
        with Image.open(output_path) as img:
            assert img.mode == "RGBA"

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
        with Image.open(output_path) as img:
            assert img.mode == "RGBA"
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
        with Image.open(output_path) as img:
            # WEBP can be RGBA
            assert img.mode in ("RGBA", "RGB")
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
        with Image.open(output_path) as img:
            # JPEG should be RGB (no alpha)
            assert img.mode == "RGB"
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
        with Image.open(output_path) as img:
            # Should be scaled down to 100x50 to fit in 100x100 while preserving 2:1 ratio
            assert img.width == 100  # noqa: PLR2004
            assert img.height == 50  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_small_image(self, temp_output_dir: str, tmp_path: Path) -> None:
        """Test with very small image."""
        # Create tiny 10x10 image
        temp_path = str(tmp_path / "test_small_image.png")
        tiny_img = Image.new("RGB", (10, 10), color="blue")
        tiny_img.save(temp_path, format="PNG")

        generator = PILRoundedPreviewGenerator(
            source_file_location=temp_path,
            preview_format="png",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.png",
            params={"max_width": 100, "max_height": 100, "corner_radius_percent": 0.5},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

        with Image.open(output_path) as img:
            # Small image should remain 10x10
            assert img.width == 10  # noqa: PLR2004
            assert img.height == 10  # noqa: PLR2004
