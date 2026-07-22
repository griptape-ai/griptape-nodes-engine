"""Coverage for VideoArtifactProvider._parse_probe_data's exception safety.

get_artifact_metadata's contract (shared with ImageArtifactProvider) is to
never raise -- callers treat None as "metadata unavailable" and fall back
gracefully. _run_ffprobe already guards every subprocess/JSON failure mode;
these tests cover the parsing step after that, which handles malformed
ffprobe output and a source file that disappears before the stat() call.
"""

import tempfile
from pathlib import Path

from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactMetadata,
    VideoArtifactProvider,
)


def _probe_data(**stream_overrides: object) -> dict:
    stream = {
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "codec_name": "h264",
        "r_frame_rate": "30/1",
        "duration": "10.5",
    }
    stream.update(stream_overrides)
    return {"streams": [stream], "format": {}}


class TestParseProbeDataSuccess:
    def test_valid_probe_data_returns_metadata(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            metadata = VideoArtifactProvider._parse_probe_data(_probe_data(), f.name)

        assert metadata == VideoArtifactMetadata(
            width=1920,
            height=1080,
            duration_seconds=10.5,
            codec="h264",
            frame_rate=30.0,
            file_size=0,
        )

    def test_no_video_stream_returns_none(self) -> None:
        probe_data = {"streams": [{"codec_type": "audio"}], "format": {}}
        assert VideoArtifactProvider._parse_probe_data(probe_data, "/some/video.mp4") is None


class TestParseProbeDataMalformedInput:
    """Malformed ffprobe output must yield None, not raise."""

    def test_non_numeric_width_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            metadata = VideoArtifactProvider._parse_probe_data(_probe_data(width="not-a-number"), f.name)
        assert metadata is None

    def test_malformed_frame_rate_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            metadata = VideoArtifactProvider._parse_probe_data(_probe_data(r_frame_rate="abc/def"), f.name)
        assert metadata is None

    def test_non_numeric_duration_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            metadata = VideoArtifactProvider._parse_probe_data(_probe_data(duration="not-a-duration"), f.name)
        assert metadata is None


class TestParseProbeDataMissingFile:
    def test_source_file_missing_at_stat_time_returns_none(self) -> None:
        # Simulates the file vanishing between the ffprobe call and stat() --
        # a real TOCTOU window, not hypothetical.
        missing_path = Path(tempfile.gettempdir()) / "definitely-does-not-exist.mp4"
        assert not missing_path.exists()

        metadata = VideoArtifactProvider._parse_probe_data(_probe_data(), str(missing_path))

        assert metadata is None
