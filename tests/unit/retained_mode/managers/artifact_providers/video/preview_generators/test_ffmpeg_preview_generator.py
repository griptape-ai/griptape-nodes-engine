"""Tests for FFmpegPreviewGenerator."""

import json
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.retained_mode.managers.artifact_providers.video.preview_generators.ffmpeg_preview_generator import (
    FFmpegPreviewGenerator,
)
from griptape_nodes.utils.async_utils import subprocess_run

try:
    _FFMPEG_PATH, _FFPROBE_PATH = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
    FFMPEG_AVAILABLE = True
except Exception:
    _FFMPEG_PATH = ""
    _FFPROBE_PATH = ""
    FFMPEG_AVAILABLE = False


@pytest.fixture
def temp_test_video() -> Generator[str, None, None]:
    """Create a temporary ProRes MOV test video using ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as f:
        temp_path = f.name

    subprocess.run(  # noqa: S603
        [
            _FFMPEG_PATH,
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=200x100:rate=1",
            "-c:v",
            "prores_ks",
            "-y",
            temp_path,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    yield temp_path

    temp_file = Path(temp_path)
    if temp_file.exists():
        temp_file.unlink()


@pytest.fixture
def temp_output_dir() -> Generator[str, None, None]:
    """Create temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def dummy_source_path() -> Generator[str, None, None]:
    """Create a dummy source path for parameter validation tests."""
    with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as f:
        temp_path = f.name
    yield temp_path
    temp_file = Path(temp_path)
    if temp_file.exists():
        temp_file.unlink()


class TestFFmpegPreviewGeneratorParameters:
    """Test parameter validation."""

    def test_invalid_max_width_negative(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that negative max_width raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": -100, "max_height": 100},
            )

    def test_invalid_max_width_zero(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that zero max_width raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 0, "max_height": 100},
            )

    def test_invalid_max_width_too_large(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that max_width > 8192 raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 8193, "max_height": 100},
            )

    def test_invalid_max_height_negative(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that negative max_height raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 100, "max_height": -100},
            )

    def test_invalid_max_height_zero(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that zero max_height raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 100, "max_height": 0},
            )

    def test_invalid_max_height_too_large(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that max_height > 8192 raises ValidationError."""
        with pytest.raises(ValidationError):
            FFmpegPreviewGenerator(
                source_file_location=dummy_source_path,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 100, "max_height": 8193},
            )

    def test_max_width_string_coercion(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that string max_width is coerced to int (Pydantic feature)."""
        generator = FFmpegPreviewGenerator(
            source_file_location=dummy_source_path,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": "100", "max_height": 100},
        )
        assert generator.params.max_width == 100  # noqa: PLR2004

    def test_valid_parameters(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """Test that valid parameters pass validation."""
        generator = FFmpegPreviewGenerator(
            source_file_location=dummy_source_path,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        assert generator.params.max_width == 150  # noqa: PLR2004
        assert generator.params.max_height == 150  # noqa: PLR2004


class TestFFmpegPreviewGeneratorClassMethods:
    """Test class methods."""

    def test_get_friendly_name(self) -> None:
        """Test get_friendly_name returns correct name."""
        assert FFmpegPreviewGenerator.get_friendly_name() == "Standard Video Preview Generation"

    def test_get_supported_source_formats(self) -> None:
        """Test get_supported_source_formats returns correct set."""
        formats = FFmpegPreviewGenerator.get_supported_source_formats()
        assert isinstance(formats, set)
        assert "mov" in formats
        assert "mp4" in formats
        assert "avi" in formats
        assert "mkv" in formats

    def test_get_supported_preview_formats(self) -> None:
        """Test get_supported_preview_formats returns correct set."""
        formats = FFmpegPreviewGenerator.get_supported_preview_formats()
        assert isinstance(formats, set)
        assert "mp4" in formats

    def test_get_parameters(self) -> None:
        """Test get_parameters returns correct Pydantic model class."""
        params_model_class = FFmpegPreviewGenerator.get_parameters()
        model_fields = params_model_class.model_fields

        assert len(model_fields) == 2  # noqa: PLR2004
        assert "max_width" in model_fields
        assert "max_height" in model_fields

        # Verify defaults
        assert model_fields["max_width"].default == 1024  # noqa: PLR2004
        assert model_fields["max_height"].default == 1024  # noqa: PLR2004


class TestFFmpegPreviewGeneratorBinaryResolution:
    """Binary-resolution failures surface from preview generation."""

    @pytest.mark.asyncio
    async def test_resolver_failure_propagates_unwrapped(self, dummy_source_path: str, temp_output_dir: str) -> None:
        """A resolver FileNotFoundError propagates verbatim, not re-wrapped in a second message."""
        generator = FFmpegPreviewGenerator(
            source_file_location=dummy_source_path,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 100, "max_height": 100},
        )

        target = (
            "griptape_nodes.retained_mode.managers.artifact_providers.video.preview_generators"
            ".ffmpeg_preview_generator.resolve_ffmpeg_binaries"
        )
        with (
            patch(target, side_effect=FileNotFoundError("boom")),
            pytest.raises(FileNotFoundError, match=r"^boom$"),
        ):
            await generator.attempt_generate_preview()


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not installed")
class TestFFmpegPreviewGeneratorGeneration:
    """Test preview generation (requires ffmpeg)."""

    @pytest.mark.asyncio
    async def test_generate_basic_preview(self, temp_test_video: str, temp_output_dir: str) -> None:
        """Test generating a basic MP4 preview from a ProRes MOV."""
        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        result_filename = await generator.attempt_generate_preview()

        assert result_filename == "output.mp4"
        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()
        assert output_path.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_generate_preserves_aspect_ratio(self, temp_test_video: str, temp_output_dir: str) -> None:
        """Test that aspect ratio is preserved during scaling."""
        # Source is 200x100 (2:1 ratio)
        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 100, "max_height": 100},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / result_filename
        assert output_path.exists()

        # Verify dimensions via ffprobe
        result = await subprocess_run(
            [
                _FFPROBE_PATH,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        probe_data = json.loads(result.stdout)
        video_stream = next(s for s in probe_data["streams"] if s["codec_type"] == "video")
        width = int(video_stream["width"])
        height = int(video_stream["height"])

        # Should be scaled to 100x50 (preserving 2:1), with even dimensions
        assert width <= 100  # noqa: PLR2004
        assert height <= 100  # noqa: PLR2004
        assert width % 2 == 0
        assert height % 2 == 0

    @pytest.mark.asyncio
    async def test_generate_source_not_found(self, temp_output_dir: str) -> None:
        """Test that FileNotFoundError is raised for missing source."""
        generator = FFmpegPreviewGenerator(
            source_file_location="/nonexistent/path/video.mov",
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 100, "max_height": 100},
        )

        with pytest.raises(FileNotFoundError):
            await generator.attempt_generate_preview()

    @pytest.mark.asyncio
    async def test_generate_creates_parent_directories(self, temp_test_video: str, temp_output_dir: str) -> None:
        """Test that parent directories are created if they don't exist."""
        nested_dir = str(Path(temp_output_dir) / "nested" / "subdir")

        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=nested_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        result_filename = await generator.attempt_generate_preview()

        output_path = Path(nested_dir) / result_filename
        assert output_path.exists()
