"""Tests for ImageArtifactProvider.detect_format magic-byte sniffing."""

from io import BytesIO

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


def _heic_bytes() -> bytes:
    """Minimal HEIC ftyp box; Pillow can't generate HEIC without pillow-heif."""
    return b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16


def _avif_bytes() -> bytes:
    """Minimal AVIF ftyp box; Pillow can't generate AVIF natively."""
    return b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16


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

    def test_ico(self) -> None:
        buf = BytesIO()
        Image.new("RGBA", (16, 16)).save(buf, format="ICO")
        assert ImageArtifactProvider.detect_format(buf.getvalue()) == "ico"

    def test_heic_via_iso_bmff_brand(self) -> None:
        """HEIC is claimed via the ISO BMFF brand without depending on pillow-heif."""
        assert ImageArtifactProvider.detect_format(_heic_bytes()) == "heic"

    def test_heif_mif1_brand_returns_heic(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16) == "heic"

    def test_avif_via_iso_bmff_brand(self) -> None:
        assert ImageArtifactProvider.detect_format(_avif_bytes()) == "avif"

    def test_riff_without_webp_marker_returns_none(self) -> None:
        """A RIFF header alone (e.g. WAV / AVI) must not be claimed as WebP."""
        assert ImageArtifactProvider.detect_format(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16) is None

    def test_unidentifiable_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"not an image") is None

    def test_short_data_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x89PNG") is None
