"""Tests for ExifMetadataDriver across OpenImageIO images, raw bytes, and duck-typed legacy objects."""

from __future__ import annotations

from typing import BinaryIO

import pytest

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import ExifMetadataDriver
from griptape_nodes.image import EXIF_ORIENTATION_TAG, Exif, ExifRational, GriptapeImage, ImageFormat, ImageMetadata
from griptape_nodes.image.containers import replace_jpeg_exif
from tests.unit.image.sample_images import BLUE, encode, solid

_EXIF_MAKE_TAG = 0x010F
_EXIF_MODEL_TAG = 0x0110


class _LegacyDuckPilJpeg:
    """Simulates a PIL.Image.Image passed by older griptape-nodes-library-standard nodes without importing PIL."""

    def __init__(self, jpeg_bytes: bytes) -> None:
        self._jpeg_bytes = jpeg_bytes
        self.format = "JPEG"
        self.info: dict[str, object] = {}

    def save(self, fp: BinaryIO, format: str | None = None, **_kwargs: object) -> None:  # noqa: A002
        assert format == "JPEG"
        fp.write(self._jpeg_bytes)


class TestExifMetadataDriver:
    @pytest.mark.parametrize("fmt", [ImageFormat.JPEG, ImageFormat.TIFF, ImageFormat.WEBP])
    def test_inject_and_extract_custom_user_comment_metadata(self, fmt: ImageFormat) -> None:
        raw = GriptapeImage.open(encode(solid(8, 8, BLUE), "png")).encode(fmt, metadata=ImageMetadata())
        driver = ExifMetadataDriver()

        injected = driver.inject_metadata(raw, {"project": "dailies", "frame": "1001"})
        extracted = driver.extract_metadata(injected)

        assert extracted["project"] == "dailies"
        assert extracted["frame"] == "1001"

    def test_preserves_standard_tags_and_formats_gps_coordinates(self) -> None:
        # Given a JPEG carrying standard Make/Model tags and GPS coordinates
        exif = Exif()
        exif[_EXIF_MAKE_TAG] = "Arri"
        exif[_EXIF_MODEL_TAG] = "Alexa 35"
        exif[EXIF_ORIENTATION_TAG] = 1
        exif.set_gps(1, "N")
        exif.set_gps(2, (ExifRational(51, 1), ExifRational(30, 1), ExifRational(26, 1)))
        exif.set_gps(3, "W")
        exif.set_gps(4, (ExifRational(0, 1), ExifRational(7, 1), ExifRational(39, 1)))
        jpeg_with_exif = replace_jpeg_exif(encode(solid(8, 8, BLUE), "jpg"), exif.to_bytes())

        # When injecting custom metadata and extracting everything back
        driver = ExifMetadataDriver()
        updated = driver.inject_metadata(jpeg_with_exif, {"slate": "A001C002"})
        extracted = driver.extract_metadata(updated)

        # Then standard tags, decimal GPS coordinates, and custom UserComment keys are all returned
        assert extracted["Make"] == "Arri"
        assert extracted["Model"] == "Alexa 35"
        assert extracted["GPS_GPSLatitudeRef"] == "N"
        assert extracted["GPS_GPSLatitude"] == "51.507222"
        assert extracted["GPS_GPSLongitudeRef"] == "W"
        assert extracted["GPS_GPSLongitude"] == "0.127500"
        assert extracted["slate"] == "A001C002"

    def test_legacy_duck_typed_pil_jpeg_works_without_pil(self) -> None:
        exif = Exif()
        exif[_EXIF_MAKE_TAG] = "Sony"
        jpeg_with_exif = replace_jpeg_exif(encode(solid(8, 8, BLUE), "jpg"), exif.to_bytes())
        legacy_obj = _LegacyDuckPilJpeg(jpeg_with_exif)

        driver = ExifMetadataDriver()
        assert driver.extract_metadata(legacy_obj)["Make"] == "Sony"
        injected = driver.inject_metadata(legacy_obj, {"shot": "020"})
        extracted = driver.extract_metadata(injected)
        assert extracted["Make"] == "Sony"
        assert extracted["shot"] == "020"
