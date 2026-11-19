"""Additional image error and metadata contract coverage."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import ExifMetadataDriver
from griptape_nodes.image import Exif, GriptapeImage, GriptapeImagePipeline, ImageFormat, ImageMetadata, PixelType
from griptape_nodes.image.path_metadata import read_path_metadata
from tests.unit.image.sample_images import encode, solid


class TestReviewCoverage:
    @pytest.mark.parametrize("value", [(), object(), 1 << 70, float("inf")])
    def test_exif_rejects_values_it_cannot_store(self, value: object) -> None:
        exif = Exif()
        with pytest.raises(ValueError, match="Failed"):
            exif[0xC001] = value

    @pytest.mark.parametrize("comment", ["plain text", "[]", "null"])
    def test_driver_ignores_non_dictionary_comments(self, comment: str) -> None:
        exif = Exif()
        exif[0x9286] = comment
        with GriptapeImage.open(encode(solid(64, 64), "jpg")) as image:
            source = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=exif))
        assert ExifMetadataDriver().extract_metadata(source) == {}

    def test_driver_formats_unknown_tags_and_gps(self) -> None:
        exif = Exif()
        exif[0xC001] = b"value"
        exif[0xC002] = (1, 2)
        exif[0x9286] = '{"number":42}'
        exif.set_gps(1, "N")
        exif.set_gps(2, (1.0, 2.0, 3.0))
        exif.set_gps(6, 4.0)
        with GriptapeImage.open(encode(solid(64, 64), "jpg")) as image:
            source = image.encode(ImageFormat.JPEG, metadata=ImageMetadata(exif=exif))
        result = ExifMetadataDriver().extract_metadata(source)
        assert result["Tag_49153"] == "value"
        assert result["Tag_49154"] == "1, 2"
        assert result["GPS_GPSLatitude"] == "1.034167"
        assert result["number"] == "42"

    def test_driver_rejects_png_exif_injection(self) -> None:
        with pytest.raises(ValueError, match="does not hold EXIF"):
            ExifMetadataDriver().inject_metadata(encode(solid(64, 64), "png"), {})

    def test_closed_image_has_empty_metadata_and_no_pixels(self) -> None:
        image = GriptapeImage.open(encode(solid(64, 64), "png"))
        image.close()
        assert image.metadata.is_empty()
        with pytest.raises(ValueError, match="closed"):
            GriptapeImagePipeline(image).end()

    def test_metadata_attachment_is_independent(self) -> None:
        exif = Exif()
        exif[0x010F] = "Camera"
        with (
            GriptapeImage.open(encode(solid(64, 64), "png")) as source,
            source.with_metadata(ImageMetadata(text={"key": "value"}, exif=exif)) as attached,
        ):
            exif[0x010F] = "Edited"
            attached_exif = attached.metadata.exif
            assert attached_exif is not None
            assert attached_exif[0x010F] == "Camera"
            assert source.metadata.text == {}

    def test_unsupported_pixel_type(self) -> None:
        with pytest.raises(ValueError, match="not a supported pixel type"):
            PixelType.from_numpy(np.dtype("bool"))

    def test_cover_rejects_invalid_dimensions(self) -> None:
        with GriptapeImage.open(encode(solid(64, 64), "png")) as image, pytest.raises(ValueError, match="positive"):
            GriptapeImagePipeline(image).cover(0, 32)

    @pytest.mark.parametrize(
        ("image_format", "data"),
        [
            (ImageFormat.PNG, b"\x89PNG\r\n\x1a\n" + b"\x00\x00\xff\xfftext"),
            (ImageFormat.JPEG, b"\xff\xd8\xff\xe1\x00\x01"),
            (ImageFormat.JPEG, b"\xff\xd8\xff\xff\xff\xd0\xff\xe1\xff\xff"),
        ],
    )
    def test_path_metadata_rejects_bad_lengths(self, tmp_path: Path, image_format: ImageFormat, data: bytes) -> None:
        path = tmp_path / "bad"
        path.write_bytes(data)
        with pytest.raises(ValueError, match="incomplete"):
            read_path_metadata(path, image_format)

    @pytest.mark.parametrize(
        ("image_format", "data"),
        [
            (ImageFormat.PNG, b"\x89PNG\r\n\x1a\n" + (b"\x00\x00\x00\x00JUNK" + bytes(4)) * 2),
            (ImageFormat.JPEG, b"\xff\xd8\xff\xe0\x00\x02"),
        ],
    )
    def test_path_metadata_rejects_too_many_chunks(
        self, tmp_path: Path, image_format: ImageFormat, data: bytes
    ) -> None:
        path = tmp_path / "many"
        path.write_bytes(data)
        with (
            patch("griptape_nodes.image.path_metadata._MAX_METADATA_CHUNKS", 1),
            pytest.raises(ValueError, match="too many"),
        ):
            read_path_metadata(path, image_format)
