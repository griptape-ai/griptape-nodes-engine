"""Tests for ImageArtifactProvider format detection and header metadata extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.image import EXIF_ORIENTATION_TAG, Exif
from griptape_nodes.image.containers import replace_jpeg_exif
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from tests.unit.image.sample_images import WHITE, encode, solid

if TYPE_CHECKING:
    from pathlib import Path


class TestImageDetectFormat:
    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            ("png", "png"),
            ("jpg", "jpg"),
            ("gif", "gif"),
            ("webp", "webp"),
            ("bmp", "bmp"),
            ("tif", "tiff"),
            ("exr", "exr"),
            ("hdr", "hdr"),
            ("dpx", "dpx"),
            ("jp2", "jp2"),
            ("pfm", "pfm"),
            ("ppm", "ppm"),
            ("sgi", "sgi"),
        ],
    )
    def test_sniffs_supported_formats(self, ext: str, expected: str) -> None:
        dtype = np.float32 if ext in ("exr", "hdr", "pfm") else np.uint8
        fill = 0.5 if dtype == np.float32 else 200
        pixels = np.full((32, 32, 3), fill, dtype=dtype)
        payload = encode(pixels, ext)
        detected = ImageArtifactProvider.detect_format(payload)
        assert detected == expected
        assert detected in ImageArtifactProvider.get_supported_formats()

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(b"8BPS\x00\x01" + b"\x00" * 16, "psd", id="psd"),
            pytest.param(b"DDS \x7c\x00\x00\x00" + b"\x00" * 16, "dds", id="dds"),
            pytest.param(b"\x80\x2a\x5f\xd7" + b"\x00" * 16, "cin", id="cin"),
        ],
    )
    def test_sniffs_psd_dds_and_cineon_headers(self, payload: bytes, expected: str) -> None:
        detected = ImageArtifactProvider.detect_format(payload)
        assert detected == expected
        assert detected in ImageArtifactProvider.get_supported_formats()

    def test_ico_not_claimed(self) -> None:
        """ICO is not in get_supported_formats(), so it must not be sniffed (GH#5614)."""
        ico_header = b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00"
        assert ImageArtifactProvider.detect_format(ico_header) is None

    def test_heic_via_iso_bmff_brand_not_claimed(self) -> None:
        """HEIC is not in get_supported_formats(), so it must not be sniffed (GH#5614)."""
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16) is None

    def test_heif_mif1_brand_not_claimed(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 16) is None

    def test_avif_via_iso_bmff_brand_not_claimed(self) -> None:
        """AVIF is not in get_supported_formats(), so it must not be sniffed (GH#5614)."""
        assert ImageArtifactProvider.detect_format(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16) is None

    def test_riff_without_webp_marker_returns_none(self) -> None:
        """A RIFF header alone (WAV / AVI) must not be claimed as WebP."""
        assert ImageArtifactProvider.detect_format(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16) is None

    def test_unidentifiable_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"not an image") is None

    def test_short_data_returns_none(self) -> None:
        assert ImageArtifactProvider.detect_format(b"\x89PNG") is None


class TestGetArtifactMetadata:
    def test_extracts_exr_float_header_metadata(self, tmp_path: Path) -> None:
        exr_path = tmp_path / "plate.exr"
        pixels = np.full((24, 48, 4), 0.25, dtype=np.float16)
        written = oiio.ImageBuf(np.ascontiguousarray(pixels)).write(str(exr_path))
        assert written

        meta = ImageArtifactProvider.get_artifact_metadata(str(exr_path))
        assert meta is not None
        assert meta.width == 48  # noqa: PLR2004
        assert meta.height == 24  # noqa: PLR2004
        assert meta.channels == 4  # noqa: PLR2004
        assert meta.color_space == "RGBA"
        assert meta.format == "EXR"

    def test_swaps_dimensions_for_exif_rotated_jpeg(self, tmp_path: Path) -> None:
        exif = Exif()
        exif[EXIF_ORIENTATION_TAG] = 6
        jpeg_bytes = replace_jpeg_exif(encode(solid(40, 20, WHITE), "jpg"), exif.to_bytes())
        jpg_path = tmp_path / "rotated.jpg"
        jpg_path.write_bytes(jpeg_bytes)

        meta = ImageArtifactProvider.get_artifact_metadata(str(jpg_path))
        assert meta is not None
        assert (meta.width, meta.height) == (20, 40)
