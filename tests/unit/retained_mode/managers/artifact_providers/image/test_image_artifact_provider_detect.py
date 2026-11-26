"""Tests for ImageArtifactProvider.detect_format magic-byte sniffing."""

from io import BytesIO

import pytest
from PIL import Image

from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)


def _pil_bytes(fmt: str, mode: str = "RGB") -> bytes:
    buf = BytesIO()
    Image.new(mode, (1, 1), color=0 if mode == "P" else "white").save(buf, format=fmt)
    return buf.getvalue()


def _png_bytes() -> bytes:
    return _pil_bytes("PNG")


def _jpeg_bytes() -> bytes:
    return _pil_bytes("JPEG")


def _gif_bytes() -> bytes:
    return _pil_bytes("GIF", mode="P")


def _ico_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (16, 16)).save(buf, format="ICO")
    return buf.getvalue()


class TestImageDetectFormat:
    @pytest.mark.parametrize(
        ("byte_sample", "expected_format"),
        [
            pytest.param(_png_bytes(), "png", id="png"),
            pytest.param(_jpeg_bytes(), "jpg", id="jpeg"),
            pytest.param(_gif_bytes(), "gif", id="gif"),
            pytest.param(_pil_bytes("WEBP"), "webp", id="webp"),
            pytest.param(_pil_bytes("BMP"), "bmp", id="bmp"),
            pytest.param(_pil_bytes("TIFF"), "tiff", id="tiff"),
        ],
    )
    def test_detects_supported_format(self, byte_sample: bytes, expected_format: str) -> None:
        assert ImageArtifactProvider.detect_format(byte_sample) == expected_format

    @pytest.mark.parametrize(
        "byte_sample",
        [
            pytest.param(_ico_bytes(), id="ico"),
            pytest.param(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16, id="heic"),
            pytest.param(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16, id="heif-mif1"),
            pytest.param(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16, id="avif"),
        ],
    )
    def test_unsupported_format_is_not_claimed(self, byte_sample: bytes) -> None:
        """Format detection declines these undeclared formats (GH#5614)."""
        assert ImageArtifactProvider.detect_format(byte_sample) is None

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
    avif / ico sniffing) is guarded by test_unsupported_format_is_not_claimed.
    """

    @pytest.mark.parametrize(
        "byte_sample",
        [
            _png_bytes(),
            _jpeg_bytes(),
            _gif_bytes(),
            _pil_bytes("WEBP"),
            _pil_bytes("BMP"),
            _pil_bytes("TIFF"),
        ],
        ids=["png", "jpeg", "gif", "webp", "bmp", "tiff"],
    )
    def test_detect_format_return_values_are_all_supported(self, byte_sample: bytes) -> None:
        detected = ImageArtifactProvider.detect_format(byte_sample)
        assert detected is not None
        assert detected in ImageArtifactProvider.get_supported_formats()
