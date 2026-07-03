"""Tests for VideoArtifactProvider.detect_format magic-byte sniffing."""

from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactProvider,
)


def _webm_bytes() -> bytes:
    return b"\x1aE\xdf\xa3" + b"\x00" * 16 + b"webm" + b"\x00" * 240


def _mkv_bytes() -> bytes:
    return b"\x1aE\xdf\xa3" + b"\x00" * 256


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16


def _mov_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 16


def _m4v_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypM4V " + b"\x00" * 16


def _m4a_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16


def _avi_bytes() -> bytes:
    return b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 16


def _gif_bytes() -> bytes:
    return b"GIF89a" + b"\x00" * 16


class TestVideoDetectFormat:
    def test_mp4_ftyp(self) -> None:
        assert VideoArtifactProvider.detect_format(_mp4_bytes()) == "mp4"

    def test_mov_qt_brand(self) -> None:
        assert VideoArtifactProvider.detect_format(_mov_bytes()) == "mov"

    def test_m4v_brand(self) -> None:
        assert VideoArtifactProvider.detect_format(_m4v_bytes()) == "m4v"

    def test_m4a_audio_brand_returns_none(self) -> None:
        """Audio-only ISO BMFF brand should not be claimed by the video sniffer."""
        assert VideoArtifactProvider.detect_format(_m4a_bytes()) is None

    def test_heic_brand_returns_none(self) -> None:
        """HEIC (image) brand should not be claimed by the video sniffer."""
        assert VideoArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16) is None

    def test_webm_ebml_with_doctype(self) -> None:
        assert VideoArtifactProvider.detect_format(_webm_bytes()) == "webm"

    def test_mkv_ebml_without_webm_doctype(self) -> None:
        assert VideoArtifactProvider.detect_format(_mkv_bytes()) == "mkv"

    def test_avi_riff(self) -> None:
        assert VideoArtifactProvider.detect_format(_avi_bytes()) == "avi"

    def test_gif_returns_gif(self) -> None:
        assert VideoArtifactProvider.detect_format(_gif_bytes()) == "gif"

    def test_short_data_returns_none(self) -> None:
        assert VideoArtifactProvider.detect_format(b"\x00\x01") is None

    def test_unidentifiable_returns_none(self) -> None:
        assert VideoArtifactProvider.detect_format(b"not a video stream") is None
