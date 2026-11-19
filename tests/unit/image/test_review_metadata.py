"""Metadata preservation regressions from PR review."""

import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import ExifMetadataDriver
from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline
from griptape_nodes.image.png_text_chunks import PNG_SIGNATURE, png_chunk
from tests.unit.image.sample_images import RED, encode, solid


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
