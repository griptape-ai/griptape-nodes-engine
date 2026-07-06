"""Video artifact provider."""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
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
    CheckpointFailure,
    CheckpointSubjectType,
)

if TYPE_CHECKING:
    from griptape_nodes.common.macro_parser import MacroVariables
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

    def check_write_permission(
        self,
        data: bytes,
        detected_format: str,
        *,
        file_name: str,
        caller_variables: MacroVariables | None = None,
    ) -> CheckpointDenial | None:
        """Gate a pending video write against the WRITE_VIDEO_CODEC checkpoint.

        Flow:
        1. Stage ``data`` at the project's SAVE_TEMP_FILE path via
           ``WriteTempFileRequest``. The temp lives in the project's temp
           directory, NOT the OS temp dir -- avoids exhausting a small
           system-wide temp partition when videos are multi-GB.
        2. Run ffprobe on the staged path to extract the primary video codec.
        3. In ``finally``, unconditionally truncate the staged file to a
           single null byte and then delete it. Truncate first, then delete:
           if delete fails for any reason, the file left behind is unusable
           as a video.
        4. If a codec was extracted, evaluate the WRITE_VIDEO_CODEC checkpoint.
           If codec extraction failed, FAIL CLOSED with a synthetic denial: an
           unclassified video write cannot be verified as compliant, so a
           legally-encumbered codec must not slip through on a broken probe.

        Args:
            data: The bytes about to be written.
            detected_format: The container extension (mp4, mov, ...).
            file_name: Filename of the caller's intended destination, used in
                error messages so the artist sees which save was blocked.
            caller_variables: The caller's macro variable bindings, threaded
                into ``WriteTempFileRequest`` so the temp filename inherits
                caller context (``node_name`` etc.) and any additional
                SAVE_TEMP_FILE slots the caller happens to bind get resolved.

        The known cost is speed (one extra full-buffer write to disk per gated
        write). Users chose this over the OS-temp-doubling problem in the
        previous approach.
        """
        codec = self._extract_codec_via_staging(
            data, detected_format, file_name=file_name, caller_variables=caller_variables
        )
        if codec is None:
            # Fail closed: if we can't identify the codec, we can't confirm the
            # write is compliant with the policy. For a gate that exists to
            # protect against legally-encumbered codecs, defaulting to permit
            # would silently bless disallowed writes whenever ffprobe hiccups.
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(detail=f"Cannot save '{file_name}': the video codec could not be verified."),
                )
            )

        return self._evaluate_codec_checkpoint(
            action=CheckpointAction.WRITE_VIDEO_CODEC,
            codec=codec,
            container_format=detected_format,
        )

    def check_read_permission(self, source_path: str) -> CheckpointDenial | None:
        """Gate a pending video read against the READ_VIDEO_CODEC checkpoint.

        Runs ffprobe on the source (no staging -- we already have a path)
        and consults the hook chain. Fails closed when the codec cannot be
        extracted: an unverifiable read is refused for the same reason writes
        are (see ``check_write_permission``).
        """
        probe_data = self._run_ffprobe(source_path)
        codec = self._codec_from_probe_data(probe_data)
        if codec is None:
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail=(f"Cannot load '{Path(source_path).name}': the video codec could not be verified.")
                    ),
                )
            )

        container_format = Path(source_path).suffix.lstrip(".").lower() or "unknown"
        return self._evaluate_codec_checkpoint(
            action=CheckpointAction.READ_VIDEO_CODEC,
            codec=codec,
            container_format=container_format,
        )

    @classmethod
    def _extract_codec_via_staging(
        cls,
        data: bytes,
        detected_format: str,
        *,
        file_name: str,
        caller_variables: MacroVariables | None,
    ) -> str | None:
        """Stage bytes at the project temp path, run ffprobe, then truncate + delete.

        Returns the extracted codec name, or ``None`` when any step failed
        (stage failure, ffprobe failure, no video stream). The caller
        translates None into a fail-closed denial that names ``file_name``.
        """
        from griptape_nodes.retained_mode.events.os_events import (  # avoid circular import
            WriteTempFileRequest,
            WriteTempFileResultSuccess,
        )

        # Start with the caller's variables (so slots like ``node_name`` land
        # in the temp filename for observability) and then apply the vet's
        # required overrides. ``file_name_base`` is a uuid so concurrent
        # codec-vet stagings never collide (SAVE_TEMP_FILE's on-collision
        # policy is OVERWRITE, which would otherwise let one probe silently
        # trample another's bytes). ``file_extension`` is the sniffed
        # container so ffprobe's extension-based dispatch picks the right
        # demuxer. Vet overrides win over caller values on those two keys --
        # a caller-supplied ``file_name_base`` would defeat the uniqueness
        # guarantee and a caller-supplied ``file_extension`` would lie to
        # ffprobe about the container.
        variables: MacroVariables = dict(caller_variables) if caller_variables else {}
        variables["file_name_base"] = uuid.uuid4().hex
        variables["file_extension"] = detected_format

        stage_result = GriptapeNodes.handle_request(WriteTempFileRequest(content=data, variables=variables))
        if not isinstance(stage_result, WriteTempFileResultSuccess):
            logger.error(
                "Attempted to stage bytes for codec verification of '%s'. Failed: %s",
                file_name,
                stage_result.result_details,
            )
            return None

        staged_path = stage_result.staged_path
        try:
            probe_data = cls._run_ffprobe(staged_path)
        finally:
            cls._truncate_and_delete(staged_path, file_name=file_name)

        return cls._codec_from_probe_data(probe_data)

    @classmethod
    def _truncate_and_delete(cls, staged_path: str, *, file_name: str) -> None:
        r"""Truncate ``staged_path`` to a single null byte, then delete it.

        Truncate first so that even if the delete fails, whatever remains on
        disk cannot be interpreted as a video: ffprobe on a 1-byte file finds
        no container header. Delete second so the normal case leaves nothing
        behind. Both steps inspect the request result (``handle_request``
        already catches all exceptions internally and returns a Failure
        payload -- no try/except needed here). A failure at either step is
        logged as an ERROR: leaving disallowed bytes on disk is a real
        problem, even if the truncate already neutralized the file's contents.

        Writing a single ``\x00`` byte in OVERWRITE mode truncates whatever
        size the staged file had (potentially multi-GB for real customer
        video assets), avoiding a large in-memory buffer and a second big
        disk write.
        """
        from griptape_nodes.retained_mode.events.base_events import ResultPayloadFailure
        from griptape_nodes.retained_mode.events.os_events import (  # avoid circular import
            DeleteFileRequest,
            DeletionBehavior,
            WriteFileRequest,
            WriteFileResultFailure,
        )

        # Use a plain WriteFileRequest (OVERWRITE is the default) so this
        # doesn't re-enter the codec vet: a lone null byte sniffs to no known
        # format and doesn't reach VideoArtifactProvider anyway.
        truncate_result = GriptapeNodes.handle_request(WriteFileRequest(file_path=staged_path, content=b"\x00"))
        if isinstance(truncate_result, (WriteFileResultFailure, ResultPayloadFailure)):
            logger.error(
                "Attempted to truncate staged video temp for '%s' at '%s'. Failed: %s",
                file_name,
                staged_path,
                truncate_result.result_details,
            )

        from griptape_nodes.retained_mode.events.os_events import DeleteFileResultFailure  # avoid circular import

        delete_result = GriptapeNodes.handle_request(
            DeleteFileRequest(
                path=staged_path,
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
            )
        )
        if isinstance(delete_result, (DeleteFileResultFailure, ResultPayloadFailure)):
            logger.error(
                "Attempted to delete staged video temp for '%s' at '%s'. Failed: %s (file has been truncated).",
                file_name,
                staged_path,
                delete_result.result_details,
            )

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
