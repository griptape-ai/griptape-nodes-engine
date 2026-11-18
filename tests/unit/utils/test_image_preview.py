"""Tests for OpenImageIO-backed image preview utilities."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image import EXIF_ORIENTATION_TAG, Exif, GriptapeImage, ImageFormat
from griptape_nodes.image.containers import replace_jpeg_exif
from griptape_nodes.utils.image_preview import (
    create_image_preview,
    create_image_preview_from_bytes,
    get_image_info,
)
from tests.unit.image.sample_images import RED, encode, solid

if TYPE_CHECKING:
    from pathlib import Path


def _decode_data_url(data_url: str) -> bytes:
    _header, payload = data_url.split(",", 1)
    return base64.b64decode(payload)


class TestImagePreviewUtilities:
    def test_create_image_preview_resizes_and_orients_file(self, tmp_path: Path) -> None:
        exif = Exif()
        exif[EXIF_ORIENTATION_TAG] = 6
        rotated_jpeg = replace_jpeg_exif(encode(solid(80, 40, RED), "jpg"), exif.to_bytes())
        jpg_path = tmp_path / "photo.jpg"
        jpg_path.write_bytes(rotated_jpeg)

        data_url = create_image_preview(jpg_path, max_width=20, max_height=20, image_format="WEBP")

        assert data_url is not None
        assert data_url.startswith("data:image/webp;base64,")
        with GriptapeImage.open(_decode_data_url(data_url)) as preview:
            assert preview.format == ImageFormat.WEBP
            assert (preview.width, preview.height) == (10, 20)

    def test_create_image_preview_from_bytes_handles_exr_and_rejects_svg(self, tmp_path: Path) -> None:
        exr_path = tmp_path / "hdr.exr"
        assert oiio.ImageBuf(np.full((32, 64, 3), 0.75, dtype=np.float32)).write(str(exr_path))

        data_url = create_image_preview_from_bytes(exr_path.read_bytes(), max_width=16, max_height=16)

        assert data_url is not None
        assert data_url.startswith("data:image/webp;base64,")
        assert create_image_preview_from_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
        svg_path = tmp_path / "icon.svg"
        svg_path.write_text("<svg></svg>", encoding="utf-8")
        assert create_image_preview(svg_path) is None

    def test_get_image_info_reports_oriented_dimensions_and_color_space(self, tmp_path: Path) -> None:
        exif = Exif()
        exif[EXIF_ORIENTATION_TAG] = 6
        jpg_path = tmp_path / "oriented.jpg"
        jpg_path.write_bytes(replace_jpeg_exif(encode(solid(100, 50, RED), "jpg"), exif.to_bytes()))

        info = get_image_info(jpg_path)

        assert info is not None
        assert info["width"] == 50  # noqa: PLR2004
        assert info["height"] == 100  # noqa: PLR2004
        assert info["format"] == ImageFormat.JPEG
        assert info["color_space"] == "RGB"
        assert info["size_bytes"] == jpg_path.stat().st_size
