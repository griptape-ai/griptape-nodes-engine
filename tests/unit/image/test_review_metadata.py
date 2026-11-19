"""Metadata preservation regressions from PR review."""

import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import ExifMetadataDriver
from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.image import Exif, GriptapeImage, GriptapeImagePipeline, ImageFormat, ImageMetadata
from griptape_nodes.image.path_metadata import read_path_extra_metadata_kinds
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, png_chunk
from tests.unit.image.sample_images import RED, encode, gray_tiff, solid


class TestMetadataReview:
    @pytest.mark.parametrize(
        "payload", [b"opaque\x00\x00broken zlib", b"opaque\x00\x00" + zlib.compress(b"x" * (2 * 1024 * 1024))]
    )
    def test_unreadable_text_survives_injection(self, payload: bytes) -> None:
        source = encode(solid(64, 64, RED), "png")
        chunk = png_chunk(b"zTXt", payload)
        source = source[:-12] + chunk + source[-12:]
        result = PngMetadataDriver().inject_metadata(source, {"workflow": "new"})
        assert chunk in result

    def test_webp_metadata_keeps_compressed_pixels(self) -> None:
        source = encode(solid(64, 64, RED), "webp")
        result = ExifMetadataDriver().inject_metadata(source, {"workflow": "new"})
        chunk = self._pixel_chunk(source)
        assert chunk in result
        again = ExifMetadataDriver().inject_metadata(result, {"workflow": "updated"})
        assert again.count(b"EXIF") == 1
        assert chunk in again

    @pytest.mark.parametrize("extension", ["png", "jpg", "webp", "tif"])
    def test_path_preview_avoids_whole_source_read(self, extension: str, tmp_path: Path) -> None:
        path = tmp_path / f"source.{extension}"
        path.write_bytes(encode(solid(64, 64, RED), extension))
        target_width = 32
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("whole-source read")),
            GriptapeImage.open(path) as image,
            GriptapeImagePipeline(image).thumbnail(target_width, target_width).end() as preview,
        ):
            assert preview.width == target_width
            assert preview.metadata.text == {}
            assert image._source is None

    @pytest.mark.parametrize(
        "source",
        [PNG_SIGNATURE + bytes(64), b"\xff\xd8\xff" + bytes(64), b"RIFF" + struct.pack("<I", 68) + b"WEBP" + bytes(64)],
    )
    def test_corrupt_known_signature_fails_at_open(self, source: bytes) -> None:
        with pytest.raises(ValueError, match="could not read"):
            GriptapeImage.open(source)

    @pytest.mark.parametrize("extension", ["png", "jpg", "webp", "tif"])
    def test_path_metadata_matches_bytes(self, extension: str, tmp_path: Path) -> None:
        exif = Exif()
        exif[0x010F] = "Camera"
        exif[0x0112] = 6
        exif.set_gps(1, "N")
        with GriptapeImage.open(encode(solid(64, 64, RED), extension)) as image:
            assert image.format is not None
            data = image.encode(image.format, metadata=ImageMetadata(exif=exif))
        path = tmp_path / f"metadata.{extension}"
        path.write_bytes(data)
        with GriptapeImage.open(path) as file_image, GriptapeImage.open(data) as byte_image:
            assert file_image.metadata == byte_image.metadata

    def test_tiff_metadata_limit_is_explicit(self, tmp_path: Path) -> None:
        exif = Exif()
        exif[0x927C] = b"large maker note" * 16
        with GriptapeImage.open(encode(solid(64, 64), "tif")) as image:
            data = image.encode(ImageFormat.TIFF, metadata=ImageMetadata(exif=exif))
        path = tmp_path / "limited.tif"
        path.write_bytes(data)
        with (
            patch("griptape_nodes.image.exif.MAX_METADATA_READ_BYTES", 100),
            GriptapeImage.open(path) as image,
            pytest.raises(ValueError, match="size limit"),
        ):
            _ = image.metadata

    def test_path_reencoding_reports_xmp_loss(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        source = encode(solid(64, 64), "jpg")
        xmp = b"http://ns.adobe.com/xap/1.0/\x00" + b"<custom/>"
        segment = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
        path = tmp_path / "xmp.jpg"
        path.write_bytes(source[:2] + segment + source[2:])
        with GriptapeImage.open(path) as image, GriptapeImagePipeline(image).thumbnail(32, 32).end() as preview:
            preview.encode(ImageFormat.WEBP)
        assert any("XMP" in record.getMessage() for record in caplog.records)

    def test_tiff_path_reports_custom_metadata_kinds(self, tmp_path: Path) -> None:
        path = tmp_path / "extra.tif"
        path.write_bytes(gray_tiff({0x02BC: (7, b"<custom/>"), 0x83BB: (7, b"iptc")}))
        assert read_path_extra_metadata_kinds(path, ImageFormat.TIFF) == ["XMP", "IPTC"]

    @pytest.mark.parametrize(
        "data", [b"bad", b"MM\x00\x2b" + bytes(4), b"II\x2a\x00" + struct.pack("<I", 8) + b"\x01\x00"]
    )
    def test_extra_tiff_metadata_handles_invalid_directories(self, tmp_path: Path, data: bytes) -> None:
        path = tmp_path / "invalid.tif"
        path.write_bytes(data)
        assert read_path_extra_metadata_kinds(path, ImageFormat.TIFF) == []

    def test_relative_open_keeps_its_original_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "source.png"
        path.write_bytes(encode(solid(64, 64, RED), "png"))
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(tmp_path)
        with GriptapeImage.open(Path("source.png")) as image:
            monkeypatch.chdir(other)
            assert image.metadata.text == {}
            assert image.width == 64  # noqa: PLR2004

    @staticmethod
    def _pixel_chunk(source: bytes) -> bytes:
        position = 12
        while position + 8 <= len(source):
            size = int.from_bytes(source[position + 4 : position + 8], "little")
            end = position + 8 + size + (size & 1)
            if source[position : position + 4] in (b"VP8 ", b"VP8L"):
                return source[position:end]
            position = end
        msg = "sample has no pixel chunk"
        raise AssertionError(msg)
