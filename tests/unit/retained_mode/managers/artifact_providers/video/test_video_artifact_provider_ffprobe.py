"""Coverage for VideoArtifactProvider._run_ffprobe binary resolution.

The codec-vetting path calls ``_run_ffprobe`` synchronously. When no ffprobe
binary can be resolved, it must fail closed (return ``None``) and log an error
whose text matches what the troubleshooting docs tell users to look for.
"""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactProvider,
)

_RESOLVE_TARGET = (
    "griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider.resolve_ffmpeg_binaries"
)


class TestRunFfprobeBinaryResolution:
    """``_run_ffprobe`` fails closed and logs when no ffprobe can be resolved."""

    def test_returns_none_and_logs_when_resolver_fails(self, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
        source_path = str(tmp_path / "example.mp4")

        with (
            patch(_RESOLVE_TARGET, side_effect=FileNotFoundError("no ffprobe found")),
            caplog.at_level(logging.ERROR, logger="griptape_nodes"),
        ):
            result = VideoArtifactProvider._run_ffprobe(source_path)

        assert result is None
        assert "Could not locate an ffprobe binary to read video metadata for" in caplog.text
