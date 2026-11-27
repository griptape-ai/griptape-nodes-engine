"""Distinct defects in numerical, spatial and encoding assertions."""

from pathlib import Path

import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO fixture tests require the optional dependency", allow_module_level=True)
from PIL import Image, JpegImagePlugin

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_assertions import (
    assert_image,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    Expectations,
)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_actual_pixels_fail(value: float, tmp_path: Path) -> None:
    """Nonfinite pixels must fail even when finite pixels match."""
    path = tmp_path / "nonfinite.tif"
    image = oiio.ImageBuf(oiio.ImageSpec(2, 1, 1, oiio.FLOAT))
    filled = oiio.ImageBufAlgo.fill(image, (0.5,))
    assert filled
    image.setpixel(1, 0, (value,))
    written = image.write(str(path))
    assert written
    expected = Expectations.model_validate(
        {
            "pixels": {
                "checks": [
                    {"kind": "all_pixels", "colour": [0.5], "max_absolute_error": 0},
                ]
            }
        }
    )
    with pytest.raises(AssertionError, match="nonfinite"):
        assert_image(path, expected, label="nonfinite")


@pytest.mark.parametrize("kind", ["sample", "region"])
def test_location_excludes_other_pixels(kind: str, tmp_path: Path) -> None:
    """A spatial check inspects exactly the declared location."""
    path = tmp_path / "spatial.png"
    with Image.new("RGB", (4, 4), "blue") as image:
        image.paste("red", (2, 2, 4, 4))
        image.save(path)
    check = {"kind": kind, "colour": [1, 0, 0], "max_absolute_error": 0}
    location = "position" if kind == "sample" else "region"
    check[location] = {"x": 2, "y": 2}
    if kind == "region":
        check[location].update(width=2, height=2)
    expected = Expectations.model_validate({"pixels": {"checks": [check]}})
    assert_image(path, expected, label="selected")
    check[location]["x"] = 0
    with pytest.raises(AssertionError, match=r"wrong.*pixel"):
        assert_image(path, Expectations.model_validate({"pixels": {"checks": [check]}}), label="wrong")
    check[location]["x"] = 4
    with pytest.raises(AssertionError, match="outside"):
        assert_image(path, Expectations.model_validate({"pixels": {"checks": [check]}}), label="outside")


@pytest.mark.parametrize("field", ["quantization", "progressive"])
def test_jpeg_encoding_rejections(field: str, tmp_path: Path) -> None:
    """Quantization and progressive checks reject differing contracts."""
    path = tmp_path / "red.jpg"
    with Image.new("RGB", (8, 8), "red") as image:
        image.save(path, quality=75)
    with Image.open(path) as image:
        assert isinstance(image, JpegImagePlugin.JpegImageFile)
        tables = image.quantization
    expected = Expectations.model_validate({"encoding": {"quantization": tables, "progressive": False}})
    assert_image(path, expected, label="correct")
    assert expected.encoding is not None
    if field == "quantization":
        assert expected.encoding.quantization is not None
        expected.encoding.quantization[0][0] += 1
    else:
        expected.encoding.progressive = True
    with pytest.raises(AssertionError, match=field):
        assert_image(path, expected, label=field)
