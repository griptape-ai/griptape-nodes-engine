"""Tests for VideoArtifactProvider._parse_probe_data turning ffprobe output into metadata."""

from pathlib import Path

import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.video import video_artifact_provider


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


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A 64-byte stand-in for the probed file, so stat() has something to measure."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


class TestParseProbeData:
    def test_valid_probe_data_returns_metadata(self, video_file: Path) -> None:
        metadata = video_artifact_provider.VideoArtifactProvider._parse_probe_data(_probe_data(), str(video_file))

        assert metadata == video_artifact_provider.VideoArtifactMetadata(
            width=1920,
            height=1080,
            duration_seconds=10.5,
            codec="h264",
            frame_rate=30.0,
            file_size=64,
        )

    def test_falls_back_to_format_duration(self, video_file: Path) -> None:
        probe_data = _probe_data(duration="0")
        probe_data["format"] = {"duration": "4.25"}

        metadata = video_artifact_provider.VideoArtifactProvider._parse_probe_data(probe_data, str(video_file))

        assert metadata is not None
        assert metadata.duration_seconds == 4.25  # noqa: PLR2004

    def test_no_video_stream_returns_none(self, video_file: Path) -> None:
        probe_data = {"streams": [{"codec_type": "audio"}], "format": {}}

        assert video_artifact_provider.VideoArtifactProvider._parse_probe_data(probe_data, str(video_file)) is None

    @pytest.mark.parametrize(
        "stream_overrides",
        [
            pytest.param({"width": "not-a-number"}, id="non-numeric width"),
            pytest.param({"height": None}, id="null height"),
            pytest.param({"r_frame_rate": "abc/def"}, id="non-numeric frame rate"),
            pytest.param({"r_frame_rate": "30/1/2"}, id="frame rate with two slashes"),
            pytest.param({"r_frame_rate": 30}, id="frame rate as a number"),
            pytest.param({"duration": "not-a-duration"}, id="non-numeric duration"),
        ],
    )
    def test_malformed_probe_output_returns_none(self, video_file: Path, stream_overrides: dict) -> None:
        metadata = video_artifact_provider.VideoArtifactProvider._parse_probe_data(
            _probe_data(**stream_overrides), str(video_file)
        )

        assert metadata is None

    def test_missing_source_file_returns_none(self, tmp_path: Path) -> None:
        # The file can disappear between the ffprobe run and the stat() call.
        missing_path = tmp_path / "gone.mp4"

        metadata = video_artifact_provider.VideoArtifactProvider._parse_probe_data(_probe_data(), str(missing_path))

        assert metadata is None
