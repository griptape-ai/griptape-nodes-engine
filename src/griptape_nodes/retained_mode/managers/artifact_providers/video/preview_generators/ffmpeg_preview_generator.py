"""FFmpeg-based video preview generator."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import PositiveInt  # noqa: TC002 - Runtime validation, not type-only
from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
    BaseArtifactPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
    BaseGeneratorParameters,
    Field,
)
from griptape_nodes.utils.async_utils import subprocess_run, to_thread

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


class FFmpegPreviewParameters(BaseGeneratorParameters):
    """Parameters for FFmpeg video preview generation."""

    max_width: PositiveInt = Field(
        default=1024,
        description="Maximum width in pixels for generated preview (1-8192)",
        editor_schema_type="integer",
        le=8192,
    )

    max_height: PositiveInt = Field(
        default=1024,
        description="Maximum height in pixels for generated preview (1-8192)",
        editor_schema_type="integer",
        le=8192,
    )


class FFmpegPreviewGenerator(BaseArtifactPreviewGenerator):
    """FFmpeg-based video preview generator.

    Converts video files to browser-playable H.264 MP4, scaled to fit within
    max_width x max_height while preserving aspect ratio.

    Output is pinned to what Chrome, Firefox and Safari all decode regardless of what the source
    carries: H.264 High profile 8-bit 4:2:0 video and stereo AAC audio, with no other streams.
    """

    def __init__(  # noqa: PLR0913
        self,
        source_file_location: str,
        preview_format: str,
        destination_preview_directory: str,
        destination_preview_file_name: str,
        params: dict[str, Any],
        *,
        engine: Engine | None = None,
    ) -> None:
        """Initialize the generator.

        Args:
            source_file_location: Path to the source video file
            preview_format: Target format (mp4)
            destination_preview_directory: Directory where the preview should be saved
            destination_preview_file_name: Filename for the preview
            params: Generator parameters (max_width, max_height)
            engine: The engine whose request bus this generator reads and writes files through

        Raises:
            ValidationError: If parameters are invalid
        """
        super().__init__(
            source_file_location,
            preview_format,
            destination_preview_directory,
            destination_preview_file_name,
            params,
            engine=engine,
        )

        # Validate and convert dict -> Pydantic model
        # Raises ValidationError if invalid
        self.params = FFmpegPreviewParameters.model_validate(params)

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name."""
        return "Standard Video Preview Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process."""
        return {"mov", "mp4", "avi", "mkv", "webm", "m4v", "flv", "wmv"}

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces."""
        return {"mp4"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return FFmpegPreviewParameters

    async def attempt_generate_preview(self) -> str:
        """Execute video preview generation.

        Converts the source video to browser-decodable H.264 MP4 scaled to fit within
        max_width x max_height, then moves it into place atomically.

        Raises:
            FileNotFoundError: If ffmpeg is not installed or source file not found
            OSError: If preview generation fails
        """
        # FAILURE CASE: ffmpeg not available
        # Run in a thread because the first call downloads and extracts the ffmpeg binary,
        # which would otherwise block the event loop long enough to disconnect WebSocket clients.
        try:
            ffmpeg_path, _ffprobe_path = await to_thread(static_ffmpeg_run.get_or_fetch_platform_executables_else_raise)
        except Exception as e:
            msg = f"Attempted to get ffmpeg binary via static-ffmpeg. Failed because: {e}"
            raise FileNotFoundError(msg) from e

        # FAILURE CASE: source file does not exist
        source_path = anyio.Path(self.source_file_location)
        if not await source_path.exists():
            msg = f"Source video file not found: {self.source_file_location}"
            raise FileNotFoundError(msg)

        destination_dir = anyio.Path(self.destination_preview_directory)
        await destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = Path(self.destination_preview_directory) / self.destination_preview_file_name

        # ffmpeg writes to a private sibling file that is renamed into place only once complete.
        # Writing the served path directly lets two generations for the same preview interleave into
        # one torn file, which still carries a valid moov atom and so gets cached as a good preview.
        # The name keeps the preview extension so ffmpeg still infers the muxer from it.
        temp_path = destination_path.with_name(f".{uuid.uuid4().hex}.{destination_path.name}")

        # Build the scale filter to fit within max dimensions while preserving aspect ratio.
        # force_divisible_by=2 ensures even dimensions (required by H.264).
        scale_filter = (
            f"scale='min({self.params.max_width},iw)':'min({self.params.max_height},ih)'"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2"
        )

        cmd = [
            ffmpeg_path,
            "-i",
            self.source_file_location,
            # Video: H.264 codec for broad browser compatibility
            "-c:v",
            "libx264",
            # Chrome, Firefox and Safari decode H.264 only up to High profile, 8-bit 4:2:0. libx264
            # otherwise inherits the source pixel format, so a 10-bit 4:2:2 source (ProRes 422,
            # DNxHR HQX) yields a High 4:2:2 stream that muxes and probes cleanly but plays nowhere.
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            # Constant Rate Factor: 0 (lossless) to 51 (worst). 23 is the default, good balance of quality and size.
            "-crf",
            "23",
            # Encoding speed vs compression tradeoff. "medium" is the default, balancing speed and file size.
            "-preset",
            "medium",
            # Apply scaling filter to fit within max dimensions
            "-vf",
            scale_filter,
            # Audio: AAC codec for broad browser compatibility
            "-c:a",
            "aac",
            # Browser AAC decoders are only dependable at up to two channels, and editorial sources
            # routinely carry 5.1 or discrete stems.
            "-ac",
            "2",
            # Drop the source's data and subtitle tracks, which ffmpeg's default stream selection
            # would otherwise carry into a preview that has no use for them.
            "-dn",
            "-sn",
            # -dn only discards input streams; the mov muxer still synthesizes a timecode track from
            # the source's timecode metadata, so a tmcd stream reappears in the output without this.
            "-write_tmcd",
            "0",
            # Move the MP4 metadata to the start of the file so the browser can begin playback before fully downloading
            "-movflags",
            "+faststart",
            # Overwrite output file without prompting
            "-y",
            str(temp_path),
        ]

        try:
            result = await subprocess_run(
                cmd,
                capture_output=True,
                text=True,
            )
        except OSError as e:
            await self._discard_partial_preview(temp_path)
            msg = f"Attempted to run ffmpeg for preview generation. Failed because: {e}"
            raise OSError(msg) from e

        # FAILURE CASE: ffmpeg exited with error
        if result.returncode != 0:
            await self._discard_partial_preview(temp_path)
            msg = f"ffmpeg failed with exit code {result.returncode}: {result.stderr}"
            raise OSError(msg)

        # FAILURE CASE: output file was not created
        if not await anyio.Path(temp_path).exists():
            msg = f"Attempted to generate a preview at {destination_path}. Failed because ffmpeg produced no output file."
            raise OSError(msg)

        # Same directory keeps the rename on one filesystem, so it is atomic: a reader sees either
        # the previous preview or this finished one.
        await anyio.Path(temp_path).replace(destination_path)

        return self.destination_preview_file_name

    async def _discard_partial_preview(self, temp_path: Path) -> None:
        """Remove the incomplete preview left behind by a failed ffmpeg run.

        Args:
            temp_path: The private file ffmpeg was writing to
        """
        await anyio.Path(temp_path).unlink(missing_ok=True)
