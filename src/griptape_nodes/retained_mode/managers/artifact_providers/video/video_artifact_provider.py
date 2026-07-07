"""Video artifact provider."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointSubjectType,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry

logger = logging.getLogger("griptape_nodes")


class VideoArtifactMetadata(BaseArtifactMetadata):
    """Metadata extracted from a video source file via ffprobe."""

    width: int
    height: int
    duration_seconds: float
    codec: str
    frame_rate: float
    file_size: int


class VideoArtifactProvider(BaseArtifactProvider):
    """Provider for video artifacts.

    Uses ffmpeg/ffprobe for metadata extraction and preview generation.
    ffmpeg must be available on the system PATH.
    """

    # Minimum bytes needed for the magic-byte sniffer below. ISO BMFF brand
    # sits at offset 8-12; EBML/RIFF headers fit comfortably inside 12 bytes.
    _SNIFF_MIN_HEADER_BYTES: ClassVar[int] = 12

    def __init__(self, registry: ProviderRegistry) -> None:
        """Initialize the video artifact provider.

        Args:
            registry: The ProviderRegistry that manages this provider
        """
        super().__init__(registry)

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Video"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"mov", "mp4", "avi", "mkv", "webm", "m4v", "flv", "wmv"}

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"mp4"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Standard Video Preview Generation"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "mp4"

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes."""
        from griptape_nodes.retained_mode.managers.artifact_providers.video.preview_generators import (
            FFmpegPreviewGenerator,
        )

        return [FFmpegPreviewGenerator]

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:  # noqa: C901, PLR0911
        """Magic-byte sniff for common video container formats."""
        if len(data) < cls._SNIFF_MIN_HEADER_BYTES:
            return None
        head = data[: cls._SNIFF_MIN_HEADER_BYTES]
        # ISO BMFF: 'ftyp' at bytes 4-8, brand at bytes 8-12.
        if head[4:8] == b"ftyp":
            brand = head[8:12]
            # Audio-only and image ISO BMFF brands are claimed by their own providers.
            if brand in (b"M4A ", b"M4B "):
                return None
            if brand in (b"heic", b"heix", b"mif1", b"heim", b"heis", b"msf1", b"avif", b"avis"):
                return None
            if brand == b"qt  ":
                return "mov"
            if brand in (b"M4V ", b"M4VH", b"M4VP"):
                return "m4v"
            return "mp4"
        # Matroska / WebM EBML header.
        if head[:4] == b"\x1aE\xdf\xa3":
            if b"webm" in data[:256]:
                return "webm"
            return "mkv"
        # AVI: RIFF....AVI .
        if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
            return "avi"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        return None

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> VideoArtifactMetadata | None:
        """Extract video metadata via ffprobe."""
        probe_data = cls._run_ffprobe(source_path)
        if probe_data is None:
            return None

        return cls._parse_probe_data(probe_data, source_path)

    def check_write_permission(self, data: bytes, detected_format: str) -> CheckpointDenial | None:
        """Gate a pending video write against the WRITE_VIDEO_CODEC checkpoint.

        Spools ``data`` to a temp file so ffprobe can seek (mp4/mov require it),
        extracts the primary video codec, and asks the authorization hook chain
        whether writing that codec is permitted in the current context. The
        temp file is removed before returning regardless of outcome.

        Falls open (returns None) when the codec cannot be extracted -- if we
        cannot identify what we are writing, we cannot make a permission
        decision. The write proceeds and any downstream policy that keys on
        codec must accept that some writes are unclassified.
        """
        codec = self._extract_codec_from_bytes(data, detected_format)
        if codec is None:
            return None

        return self._evaluate_codec_checkpoint(
            action=CheckpointAction.WRITE_VIDEO_CODEC,
            codec=codec,
            container_format=detected_format,
        )

    def check_read_permission(self, source_path: str) -> CheckpointDenial | None:
        """Gate a pending video read against the READ_VIDEO_CODEC checkpoint.

        Runs ffprobe on the source (no spooling -- we already have a path)
        and consults the hook chain. Falls open when the codec cannot be
        extracted, mirroring ``check_write_permission``.
        """
        probe_data = self._run_ffprobe(source_path)
        codec = self._codec_from_probe_data(probe_data)
        if codec is None:
            return None

        container_format = Path(source_path).suffix.lstrip(".").lower() or "unknown"
        return self._evaluate_codec_checkpoint(
            action=CheckpointAction.READ_VIDEO_CODEC,
            codec=codec,
            container_format=container_format,
        )

    @classmethod
    def _extract_codec_from_bytes(cls, data: bytes, detected_format: str) -> str | None:
        """Spool bytes to a temp file, run ffprobe, and return the primary video codec."""
        with tempfile.NamedTemporaryFile(suffix=f".{detected_format}", delete=False) as spool:
            spool.write(data)
            spool_path = spool.name

        try:
            probe_data = cls._run_ffprobe(spool_path)
        finally:
            Path(spool_path).unlink(missing_ok=True)

        return cls._codec_from_probe_data(probe_data)

    @staticmethod
    def _codec_from_probe_data(probe_data: dict | None) -> str | None:
        """Pull the first video stream's codec_name from an ffprobe payload."""
        if probe_data is None:
            return None
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                codec = stream.get("codec_name")
                if isinstance(codec, str) and codec:
                    return codec
        return None

    @staticmethod
    def _evaluate_codec_checkpoint(
        action: CheckpointAction, codec: str, container_format: str
    ) -> CheckpointDenial | None:
        """Build the checkpoint and ask the event manager's hook chain for a verdict."""
        checkpoint = AuthorizationCheckpoint(
            action=action,
            subject_type=CheckpointSubjectType.VIDEO_CODEC,
            subject_id=codec,
            attributes={CheckpointAttribute.CONTAINER_FORMAT: container_format},
        )
        return GriptapeNodes.EventManager().evaluate_authorization_checkpoint(checkpoint)

    @classmethod
    def _run_ffprobe(cls, source_path: str) -> dict | None:
        """Run ffprobe on a video file and return parsed JSON output."""
        try:
            _ffmpeg_path, ffprobe_path = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        except Exception:
            logger.warning("Attempted to get ffprobe binary via static-ffmpeg. Failed to fetch platform executables.")
            return None

        try:
            result = subprocess.run(  # noqa: S603
                [
                    ffprobe_path,
                    # Suppress all log output
                    "-v",
                    "quiet",
                    # Output as JSON for easy parsing
                    "-print_format",
                    "json",
                    # Include per-stream info (codec, dimensions, frame rate, etc.)
                    "-show_streams",
                    # Only the first video stream
                    "-select_streams",
                    "v:0",
                    # Include container-level info (duration, size, etc.)
                    "-show_format",
                    source_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Attempted to probe video metadata for '%s'. ffprobe timed out.", source_path)
            return None
        except subprocess.CalledProcessError as e:
            logger.warning("Attempted to probe video metadata for '%s'. ffprobe failed: %s", source_path, e.stderr)
            return None
        except OSError as e:
            logger.warning("Attempted to run ffprobe for '%s'. Failed because: %s", source_path, e)
            return None

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warning("Attempted to parse ffprobe output for '%s'. Failed to decode JSON.", source_path)
            return None

    @classmethod
    def _parse_probe_data(cls, probe_data: dict, source_path: str) -> VideoArtifactMetadata | None:
        """Parse ffprobe JSON output into VideoArtifactMetadata."""
        # Find the first video stream
        video_stream = None
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_stream = stream
                break

        if video_stream is None:
            logger.warning("Attempted to find video stream in '%s'. No video stream found.", source_path)
            return None

        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec = video_stream.get("codec_name", "unknown")

        # Parse frame rate from r_frame_rate (e.g., "30/1" or "24000/1001")
        frame_rate = 0.0
        r_frame_rate = video_stream.get("r_frame_rate", "0/1")
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            if int(den) != 0:
                frame_rate = int(num) / int(den)

        # Duration from stream or format level
        duration_seconds = float(video_stream.get("duration", 0.0))
        if duration_seconds == 0.0:
            format_info = probe_data.get("format", {})
            duration_seconds = float(format_info.get("duration", 0.0))

        file_size = Path(source_path).stat().st_size

        return VideoArtifactMetadata(
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            codec=codec,
            frame_rate=round(frame_rate, 3),
            file_size=file_size,
        )
