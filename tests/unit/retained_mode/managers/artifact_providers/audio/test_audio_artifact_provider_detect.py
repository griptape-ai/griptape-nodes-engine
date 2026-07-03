"""Tests for AudioArtifactProvider.detect_format magic-byte sniffing."""

from griptape_nodes.retained_mode.managers.artifact_providers.audio.audio_artifact_provider import (
    AudioArtifactProvider,
)


def _mp3_bytes() -> bytes:
    return b"ID3" + b"\x00" * 16


def _wav_bytes() -> bytes:
    return b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16


def _flac_bytes() -> bytes:
    return b"fLaC" + b"\x00" * 32


def _ogg_bytes() -> bytes:
    return b"OggS" + b"\x00" * 124


def _opus_bytes() -> bytes:
    return b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 88


def _m4a_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16


def _m4b_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypM4B " + b"\x00" * 16


class TestAudioDetectFormat:
    def test_mp3_id3(self) -> None:
        assert AudioArtifactProvider.detect_format(_mp3_bytes()) == "mp3"

    def test_mp3_mpeg_frame_sync(self) -> None:
        assert AudioArtifactProvider.detect_format(b"\xff\xfb" + b"\x00" * 32) == "mp3"

    def test_aac_adts_sync(self) -> None:
        # ADTS frame: 12 sync bits + layer=00 + protection bit. 0xFFF1 is
        # MPEG-4 ADTS without CRC; 0xFFF9 is MPEG-2 ADTS without CRC.
        assert AudioArtifactProvider.detect_format(b"\xff\xf1" + b"\x00" * 32) == "aac"
        assert AudioArtifactProvider.detect_format(b"\xff\xf9" + b"\x00" * 32) == "aac"

    def test_wav_riff_wave(self) -> None:
        assert AudioArtifactProvider.detect_format(_wav_bytes()) == "wav"

    def test_flac(self) -> None:
        assert AudioArtifactProvider.detect_format(_flac_bytes()) == "flac"

    def test_ogg(self) -> None:
        assert AudioArtifactProvider.detect_format(_ogg_bytes()) == "ogg"

    def test_ogg_with_opus_codec_returns_opus(self) -> None:
        assert AudioArtifactProvider.detect_format(_opus_bytes()) == "opus"

    def test_m4a_iso_bmff(self) -> None:
        assert AudioArtifactProvider.detect_format(_m4a_bytes()) == "m4a"

    def test_m4b_iso_bmff(self) -> None:
        assert AudioArtifactProvider.detect_format(_m4b_bytes()) == "m4b"

    def test_short_data_returns_none(self) -> None:
        assert AudioArtifactProvider.detect_format(b"\x00\x01") is None

    def test_unidentifiable_returns_none(self) -> None:
        assert AudioArtifactProvider.detect_format(b"random opaque blob") is None
