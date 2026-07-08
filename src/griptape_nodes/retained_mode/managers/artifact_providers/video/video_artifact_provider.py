"""Video artifact provider."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
    WriteVettingPolicy,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointFailure,
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

    @staticmethod
    def get_write_vetting_policy() -> WriteVettingPolicy | None:
        """Videos need a filesystem path -- ffprobe reads containers, not raw buffers."""
        return WriteVettingPolicy.FROM_PATH

    def check_write_format_from_path(
        self,
        source_path: str,
        detected_format: str,
    ) -> CheckpointDenial | None:
        """Gate a pending video write against the WRITE_VIDEO_CODEC checkpoint.

        Runs ffprobe on the OSManager-staged path to extract the primary
        video codec and evaluates the ``WRITE_VIDEO_CODEC`` checkpoint. OSManager
        owns the staged file's lifecycle (create + truncate + delete); this
        method must not write to or delete ``source_path``.

        Fails closed when the codec cannot be extracted -- an unclassified
        write cannot be verified as compliant, so a legally-encumbered codec
        must not slip through on a broken probe.
        """
        return self._check_codec(
            source_path,
            action=CheckpointAction.WRITE_VIDEO_CODEC,
            container_format=detected_format,
        )

    def check_read_permission(self, source_path: str) -> CheckpointDenial | None:
        """Gate a pending video read against the READ_VIDEO_CODEC checkpoint.

        Runs ffprobe on the source (no staging -- we already have a path)
        and consults the hook chain. Fails closed when the codec cannot be
        extracted: an unverifiable read is refused for the same reason writes
        are.
        """
        container_format = Path(source_path).suffix.lstrip(".").lower() or "unknown"
        return self._check_codec(
            source_path,
            action=CheckpointAction.READ_VIDEO_CODEC,
            container_format=container_format,
        )

    @classmethod
    def _check_codec(
        cls,
        source_path: str,
        *,
        action: CheckpointAction,
        container_format: str,
    ) -> CheckpointDenial | None:
        """Probe ``source_path`` for its video codecs and evaluate ``action``.

        Evaluates every video stream in the container. Common containers
        (mp4, mov, mkv) can carry more than one video stream -- a main
        video plus an alpha channel, a HEIF-style file with a thumbnail
        stream in a different codec, an editorial file with multiple
        angles. Refusing on stream 0 alone would let a disallowed codec
        slip through by riding along on a later stream.

        The checkpoint contract stays "one codec per call", so we
        evaluate each stream's codec independently and return the first
        denial (or ``None`` if every stream is allowed). Hooks don't
        need to know a file might have several.

        Fail-closed on unverifiable codecs: if ffprobe cannot identify any
        video stream at all, return a synthetic denial rather than allow the
        operation. The denial detail names "probe unavailable or failed --
        see server logs" so an artist's bug report can be triaged apart
        from an actual codec denial (``_run_ffprobe`` logs the underlying
        cause at ERROR). The caller (OSManager on the write side, library
        code on the read side) is expected to wrap this detail with any
        file-name framing.
        """
        probe_data = cls._run_ffprobe(source_path)
        codecs = cls._codecs_from_probe_data(probe_data)
        if not codecs:
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail="The video codec could not be verified (probe unavailable or failed -- see server logs)."
                    ),
                )
            )

        for codec in codecs:
            denial = cls._evaluate_codec_checkpoint(
                action=action,
                codec=codec,
                container_format=container_format,
            )
            if denial is not None:
                return denial
        return None

    @staticmethod
    def _codecs_from_probe_data(probe_data: dict | None) -> list[str]:
        """Pull every video stream's codec_name from an ffprobe payload, in stream order.

        Returns an empty list when ``probe_data`` is None (probe failed) or
        when the container has no video streams. Duplicate codec names are
        preserved: the checkpoint evaluation is idempotent and a
        two-stream ``[h264, h264]`` file should not silently be treated
        as a single-stream file.
        """
        if probe_data is None:
            return []
        codecs: list[str] = []
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            codec = stream.get("codec_name")
            if isinstance(codec, str) and codec:
                codecs.append(codec)
        return codecs

    @staticmethod
    def _evaluate_codec_checkpoint(
        action: CheckpointAction, codec: str, container_format: str
    ) -> CheckpointDenial | None:
        """Build the checkpoint and ask the event manager's hook chain for a verdict.

        Populates both ``subject_id`` and ``attributes[ID]`` with the codec so hooks
        that key on ``attributes["id"]`` (the convention across model-access queries)
        see the same fact here. Without the ``ID`` attribute the enforcement path
        would fall open silently for any hook written to the model-access shape,
        while the query path in ``AccessManager.on_query_codec_access_request`` --
        which DOES set ``ID`` -- would still deny.
        """
        checkpoint = AuthorizationCheckpoint(
            action=action,
            subject_type=CheckpointSubjectType.VIDEO_CODEC,
            subject_id=codec,
            attributes={
                CheckpointAttribute.ID: codec,
                CheckpointAttribute.CONTAINER_FORMAT: container_format,
            },
        )
        return GriptapeNodes.EventManager().evaluate_authorization_checkpoint(checkpoint)

    @classmethod
    def _run_ffprobe(cls, source_path: str) -> dict | None:
        """Run ffprobe on a video file and return parsed JSON output.

        Every failure surface below logs at ERROR because these are the
        situations where the codec-vet fails closed and the user sees a
        video I/O denial: operators need to distinguish "policy denied
        this codec" (no log) from "ffprobe couldn't run" (this log).
        Bumping to ERROR keeps these visible in log tailers that filter
        to ERROR+.
        """
        try:
            _ffmpeg_path, ffprobe_path = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        except Exception as exc:
            logger.error(
                "Attempted to get ffprobe binary via static-ffmpeg for '%s'. Failed to fetch platform executables: %s",
                source_path,
                exc,
            )
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
                    # for EVERY stream in the container. Do NOT filter with
                    # ``-select_streams v:0`` -- containers can carry multiple
                    # video streams (main + alpha, main + thumbnail,
                    # multi-angle editorial) and codec policy has to see
                    # them all, not just stream 0.
                    "-show_streams",
                    # Include container-level info (duration, size, etc.)
                    "-show_format",
                    source_path,
                ],
                capture_output=True,
                text=True,
                # Runaway-probe backstop, not a work budget. ffprobe reads
                # only container headers (not the full file), so it should
                # complete well under a second on local disk regardless of
                # file size. A wedge past this ceiling (malformed header,
                # hung fuse mount, pathological box structure) fails closed
                # -- the right outcome for a security gate.
                timeout=30,
                check=True,
            )
        except subprocess.TimeoutExpired:
            logger.error("Attempted to probe video metadata for '%s'. ffprobe timed out.", source_path)
            return None
        except subprocess.CalledProcessError as e:
            logger.error("Attempted to probe video metadata for '%s'. ffprobe failed: %s", source_path, e.stderr)
            return None
        except OSError as e:
            logger.error("Attempted to run ffprobe for '%s'. Failed because: %s", source_path, e)
            return None

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error("Attempted to parse ffprobe output for '%s'. Failed to decode JSON.", source_path)
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
