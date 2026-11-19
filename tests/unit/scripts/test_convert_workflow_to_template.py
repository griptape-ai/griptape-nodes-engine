"""Tests for scripts/convert_workflow_to_template.py image thumbnail cover-cropping."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.image import GriptapeImage, ImageFormat
from tests.unit.image.sample_images import BLUE, RED, encode, solid

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "convert_workflow_to_template.py"


def _load_process_image():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("convert_workflow_to_template", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.process_image


class TestConvertWorkflowTemplateProcessImage:
    def test_cover_crops_rectangular_image_to_512x512_webp(self, tmp_path: Path) -> None:
        process_image = _load_process_image()
        source = tmp_path / "banner.png"
        source.write_bytes(encode(solid(800, 200, RED), "png"))
        output = tmp_path / "thumbs" / "banner.webp"

        process_image(source, output)

        with GriptapeImage.open(output) as img:
            assert img.format == ImageFormat.WEBP
            assert (img.width, img.height) == (512, 512)

    def test_partial_alpha_keeps_color(self, tmp_path: Path) -> None:
        process_image = _load_process_image()
        source = tmp_path / "transparent.png"
        source.write_bytes(encode(solid(600, 600, (255, 0, 0, 128)), "png"))
        output = tmp_path / "transparent.webp"
        process_image(source, output)
        with GriptapeImage.open(output) as image:
            pixels = image._decoded().get_pixels(oiio.UINT8)
            assert isinstance(pixels, np.ndarray)
            np.testing.assert_allclose(pixels[256, 256], (255, 0, 0, 128), atol=3)

    def test_copies_gif_unchanged(self, tmp_path: Path) -> None:
        process_image = _load_process_image()
        gif_bytes = encode(solid(16, 16, BLUE), "gif")
        source = tmp_path / "anim.gif"
        source.write_bytes(gif_bytes)
        output = tmp_path / "thumbs" / "anim.gif"

        process_image(source, output)

        assert output.read_bytes() == gif_bytes

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        process_image = _load_process_image()
        with pytest.raises(ValueError, match="Image file not found"):
            process_image(tmp_path / "missing.png", tmp_path / "out.webp")
