"""Tests for FFmpegPreviewGenerator."""

import asyncio
import json
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.retained_mode.managers.artifact_providers.video.preview_generators import (
    ffmpeg_preview_generator,
)
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


async def _entry_names(directory: str) -> set[str]:
    """Return the names of every entry in a directory."""
    return {entry.name async for entry in anyio.Path(directory).iterdir()}


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
def temp_prores_video_with_extra_streams() -> Generator[str, None, None]:
    """Create a 10-bit 4:2:2 ProRes MOV carrying 5.1 audio and a timecode track."""
    with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as f:
        temp_path = f.name

    subprocess.run(  # noqa: S603
        [
            _FFMPEG_PATH,
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=200x100:rate=25",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=duration=1:sample_rate=48000",
            "-af",
            "pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "3",
            "-pix_fmt",
            "yuv422p10le",
            "-c:a",
            "pcm_s24le",
            "-timecode",
            "01:00:00:00",
            "-y",
            temp_path,
        ],
        capture_output=True,
        timeout=60,
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
    async def test_generate_forces_browser_decodable_streams(
        self, temp_prores_video_with_extra_streams: str, temp_output_dir: str
    ) -> None:
        """Test that a 10-bit 4:2:2 / 5.1 / timecode source is normalized to what browsers decode."""
        generator = FFmpegPreviewGenerator(
            source_file_location=temp_prores_video_with_extra_streams,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        result_filename = await generator.attempt_generate_preview()
        output_path = Path(temp_output_dir) / result_filename

        result = await subprocess_run(
            [_FFPROBE_PATH, "-v", "error", "-print_format", "json", "-show_streams", str(output_path)],
            capture_output=True,
            text=True,
        )
        streams = json.loads(result.stdout)["streams"]

        video_stream = next(s for s in streams if s["codec_type"] == "video")
        assert video_stream["codec_name"] == "h264"
        assert video_stream["pix_fmt"] == "yuv420p"
        assert video_stream["profile"] == "High"

        audio_stream = next(s for s in streams if s["codec_type"] == "audio")
        assert audio_stream["codec_name"] == "aac"
        assert audio_stream["channels"] == 2  # noqa: PLR2004

        # The source timecode track must not ride along into the preview.
        assert [s["codec_type"] for s in streams if s["codec_type"] not in {"video", "audio"}] == []

    @pytest.mark.asyncio
    async def test_generate_leaves_no_scratch_files(self, temp_test_video: str, temp_output_dir: str) -> None:
        """Test that a successful generation leaves only the finished preview behind."""
        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        await generator.attempt_generate_preview()

        assert await _entry_names(temp_output_dir) == {"output.mp4"}

    @pytest.mark.asyncio
    async def test_failed_generation_preserves_existing_preview(
        self, temp_test_video: str, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a generation failing mid-encode does not clobber the preview being served.

        The failure has to arrive after ffmpeg has opened its output, since that is the case that
        would truncate the served file. A source ffmpeg rejects outright never opens an output at all,
        so it cannot tell writing the destination apart from writing a scratch file.
        """
        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )
        await generator.attempt_generate_preview()

        output_path = Path(temp_output_dir) / "output.mp4"
        original_bytes = await anyio.Path(output_path).read_bytes()

        async def fail_after_opening_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            await anyio.Path(cmd[-1]).write_bytes(b"truncated")
            return subprocess.CompletedProcess(cmd, 1, "", "Conversion failed!")

        monkeypatch.setattr(ffmpeg_preview_generator, "subprocess_run", fail_after_opening_output)

        with pytest.raises(OSError, match="ffmpeg exited with code"):
            await generator.attempt_generate_preview()

        assert await anyio.Path(output_path).read_bytes() == original_bytes
        assert await _entry_names(temp_output_dir) == {"output.mp4"}

    @pytest.mark.asyncio
    async def test_ffmpeg_is_never_pointed_at_the_served_path(
        self, temp_test_video: str, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that ffmpeg writes a scratch sibling rather than the path the editor serves.

        This is the deterministic guard for the interleaving bug. Asserting on the bytes of a raced
        output cannot be: whichever writer finishes last usually still produces a complete file, so a
        torn result only shows up in a minority of runs.
        """
        recorded_cmds: list[list[str]] = []

        async def fake_subprocess_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(cmd)
            await anyio.Path(cmd[-1]).write_bytes(b"encoded output")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(ffmpeg_preview_generator, "subprocess_run", fake_subprocess_run)

        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        await generator.attempt_generate_preview()

        destination = Path(temp_output_dir) / "output.mp4"
        ffmpeg_output_path = Path(recorded_cmds[0][-1])

        assert ffmpeg_output_path != destination
        # Same directory, otherwise the rename crosses filesystems and stops being atomic.
        assert ffmpeg_output_path.parent == destination.parent
        assert destination.read_bytes() == b"encoded output"
        assert await _entry_names(temp_output_dir) == {"output.mp4"}

    @pytest.mark.asyncio
    async def test_failed_rename_leaves_no_scratch_file(
        self, temp_test_video: str, temp_output_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a rename failure does not orphan the scratch file.

        Serving a preview can hold the destination open on Windows, so the rename is a real failure
        path and nothing in the repo sweeps the previews directory.
        """

        async def failing_replace(self: anyio.Path, _target: object) -> None:  # noqa: ARG001
            msg = "Access is denied"
            raise PermissionError(msg)

        monkeypatch.setattr(anyio.Path, "replace", failing_replace)

        generator = FFmpegPreviewGenerator(
            source_file_location=temp_test_video,
            preview_format="mp4",
            destination_preview_directory=temp_output_dir,
            destination_preview_file_name="output.mp4",
            params={"max_width": 150, "max_height": 150},
        )

        with pytest.raises(PermissionError):
            await generator.attempt_generate_preview()

        assert await _entry_names(temp_output_dir) == set()

    @pytest.mark.asyncio
    async def test_concurrent_generation_produces_decodable_preview(
        self, temp_test_video: str, temp_output_dir: str
    ) -> None:
        """Test that two generations racing on one destination still yield a decodable preview."""
        generators = [
            FFmpegPreviewGenerator(
                source_file_location=temp_test_video,
                preview_format="mp4",
                destination_preview_directory=temp_output_dir,
                destination_preview_file_name="output.mp4",
                params={"max_width": 150, "max_height": 150},
            )
            for _ in range(2)
        ]

        await asyncio.gather(*(generator.attempt_generate_preview() for generator in generators))

        output_path = Path(temp_output_dir) / "output.mp4"
        result = await subprocess_run(
            [_FFPROBE_PATH, "-v", "error", "-print_format", "json", "-show_streams", str(output_path)],
            capture_output=True,
            text=True,
        )
        streams = json.loads(result.stdout)["streams"]
        video_stream = next(s for s in streams if s["codec_type"] == "video")

        # A torn file still parses as MP4, but its H.264 extradata does not, so pix_fmt reads back as
        # "unknown" and the decoder logs NAL unit errors on stderr.
        assert result.stderr == ""
        assert video_stream["pix_fmt"] != "unknown"
        assert await _entry_names(temp_output_dir) == {"output.mp4"}

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
