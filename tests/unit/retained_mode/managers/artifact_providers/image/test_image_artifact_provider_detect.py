"""Tests for ImageArtifactProvider: format detection by magic bytes, and metadata read from the header."""

from pathlib import Path

import pytest

from griptape_nodes.image import GriptapeImage
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from tests.unit.image.sample_images import encode, solid


def _png_bytes() -> bytes:
    return encode(solid(1, 1), "png")


def _jpeg_bytes() -> bytes:
    return encode(solid(1, 1), "jpg")


def _gif_bytes() -> bytes:
    return encode(solid(1, 1), "gif")


class TestImageDetectFormat:
    def test_png(self) -> None:
        assert ImageArtifactProvider.detect_format(_png_bytes()) == "png"

    def test_jpeg(self) -> None:
        assert ImageArtifactProvider.detect_format(_jpeg_bytes()) == "jpg"

    def test_gif(self) -> None:
        assert ImageArtifactProvider.detect_format(_gif_bytes()) == "gif"

    def test_webp(self) -> None:
        assert ImageArtifactProvider.detect_format(encode(solid(1, 1), "webp")) == "webp"

    def test_bmp(self) -> None:
        assert ImageArtifactProvider.detect_format(encode(solid(1, 1), "bmp")) == "bmp"

    def test_tiff(self) -> None:
        assert ImageArtifactProvider.detect_format(encode(solid(1, 1), "tif")) == "tiff"

    def test_ico_not_claimed(self) -> None:
        """ICO is not in get_supported_formats(), so it must not be sniffed either (GH#5614)."""
        assert ImageArtifactProvider.detect_format(encode(solid(16, 16, (0, 0, 0, 0)), "ico")) is None

    def test_heic_via_iso_bmff_brand_not_claimed(self) -> None:
        """HEIC is not in get_supported_formats(), so it must not be sniffed either (GH#5614)."""
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16) is None

    def test_heif_mif1_brand_not_claimed(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16) is None

    def test_avif_via_iso_bmff_brand_not_claimed(self) -> None:
        """AVIF is not in get_supported_formats(), so it must not be sniffed either (GH#5614)."""
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16) is None

    def test_riff_without_webp_marker_returns_none(self) -> None:
        """A RIFF header alone (e.g. WAV / AVI) must not be claimed as WebP."""
        assert ImageArtifactProvider.detect_format(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16) is None

    def test_unidentifiable_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"not an image") is None

    def test_short_data_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x89PNG") is None


class TestDetectFormatContract:
    """Every *sampled* return value of detect_format() must be a declared format.

    This does not catch a new sniffing branch that returns an undeclared format
    with no sample here to exercise it - that shape of drift (GH#5614's heic /
    avif / ico sniffing) is guarded by the *_not_claimed tests above instead.
    """

    @pytest.mark.parametrize(
        "byte_sample",
        [
            _png_bytes(),
            _jpeg_bytes(),
            _gif_bytes(),
            encode(solid(1, 1), "webp"),
            encode(solid(1, 1), "bmp"),
            encode(solid(1, 1), "tif"),
        ],
        ids=["png", "jpeg", "gif", "webp", "bmp", "tiff"],
    )
    def test_detect_format_return_values_are_all_supported(self, byte_sample: bytes) -> None:
        detected = ImageArtifactProvider.detect_format(byte_sample)
        assert detected is not None
        assert detected in ImageArtifactProvider.get_supported_formats()


class TestArtifactMetadata:
    @pytest.mark.parametrize(
        ("extension", "expected"), [("jpg", "JPEG"), ("tif", "TIFF"), ("png", "PNG"), ("exr", "EXR")]
    )
    def test_reports_the_detected_format(self, tmp_path: Path, extension: str, expected: str) -> None:
        path = tmp_path / f"image.{extension}"
        path.write_bytes(encode(solid(6, 4), extension))

        metadata = ImageArtifactProvider.get_artifact_metadata(str(path))

        assert metadata is not None
        assert metadata.format == expected
        assert (metadata.width, metadata.height, metadata.channels) == (6, 4, 3)

    def test_reads_the_header_without_decoding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "image.jpg"
        path.write_bytes(encode(solid(6, 4), "jpg"))

        def fail_decode(_self: GriptapeImage) -> None:
            msg = "decoded"
            raise AssertionError(msg)

        monkeypatch.setattr(GriptapeImage, "_decoded", fail_decode)

        metadata = ImageArtifactProvider.get_artifact_metadata(str(path))

        assert metadata is not None
        assert (metadata.width, metadata.height) == (6, 4)
