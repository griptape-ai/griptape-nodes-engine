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


class TestImageDetectFormat:
    def test_png(self) -> None:
        assert ImageArtifactProvider.detect_format(_png_bytes()) == "png"

    def test_jpeg(self) -> None:
        assert ImageArtifactProvider.detect_format(_jpeg_bytes()) == "jpg"

    def test_gif(self) -> None:
        assert ImageArtifactProvider.detect_format(_gif_bytes()) == "gif"

    def test_webp(self) -> None:
        assert ImageArtifactProvider.detect_format(_pil_bytes("WEBP")) == "webp"

    def test_bmp(self) -> None:
        assert ImageArtifactProvider.detect_format(_pil_bytes("BMP")) == "bmp"

    def test_tiff(self) -> None:
        assert ImageArtifactProvider.detect_format(_pil_bytes("TIFF")) == "tiff"

    def test_ico_not_claimed(self) -> None:
        """ICO is not in get_supported_formats(), so it must not be sniffed either (GH#5614)."""
        buf = BytesIO()
        Image.new("RGBA", (16, 16)).save(buf, format="ICO")
        assert ImageArtifactProvider.detect_format(buf.getvalue()) is None

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
