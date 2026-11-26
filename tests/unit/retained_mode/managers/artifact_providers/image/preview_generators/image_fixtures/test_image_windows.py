"""EXR windows retain native storage coordinates and local pixel contracts."""

from pathlib import Path

import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO fixture tests require the optional dependency", allow_module_level=True)
from pydantic import ValidationError

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_assertions import (
    assert_image,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_generation import (
    read_image,
    write_variant,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    Expectations,
    ImageSpecification,
)


def window_specification_data() -> dict:
    """Provide data dimensions distinct from the display dimensions."""
    return {
        "id": "windows",
        "source": {
            "dimensions": {"width": 4, "height": 2},
            "data_origin": {"x": -3, "y": 5},
            "display_window": {"x": -5, "y": -2, "width": 12, "height": 10},
            "channels": ["Y"],
            "pixels": {"pattern": "solid", "colour": [0.5]},
        },
        "variants": [{"id": "exr", "format": "exr", "sample_type": "float32"}],
    }


def test_window_schema_signed_coordinates_and_defaults() -> None:
    """Window positions are signed, but pixel check positions remain local."""
    data = window_specification_data()
    specification = ImageSpecification.model_validate(data)
    source = specification.source
    assert source.data_origin.model_dump() == {"x": -3, "y": 5}
    assert source.data_window.model_dump() == {"x": -3, "y": 5, "width": 4, "height": 2}
    assert source.resolved_display_window.model_dump() == data["source"]["display_window"]
    del data["source"]["display_window"]
    source = ImageSpecification.model_validate(data).source
    assert source.resolved_display_window == source.data_window
    del data["source"]["data_origin"]
    source = ImageSpecification.model_validate(data).source
    assert source.data_window.model_dump() == {"x": 0, "y": 0, "width": 4, "height": 2}
    assert source.resolved_display_window == source.data_window
    with pytest.raises(ValidationError):
        Expectations.model_validate(
            {
                "pixels": {
                    "checks": [
                        {"kind": "sample", "position": {"x": -1, "y": 0}, "colour": [0.5], "max_absolute_error": 0}
                    ]
                }
            }
        )


@pytest.mark.parametrize("field", ["data_origin", "display_window"])
@pytest.mark.parametrize("format_name", ["png", "jpeg", "webp", "tiff", "hdr", "dpx"])
def test_explicit_windows_require_exr(field: str, format_name: str) -> None:
    """Even an explicit zero origin is not a portable format control."""
    data = window_specification_data()
    data["source"]["channels"] = ["R", "G", "B"]
    data["source"]["pixels"]["colour"] = [0.5, 0.5, 0.5]
    del data["source"]["data_origin"]
    del data["source"]["display_window"]
    if field == "data_origin":
        data["source"][field] = {"x": 0, "y": 0}
    else:
        data["source"][field] = {"x": 0, "y": 0, "width": 4, "height": 2}
    data["variants"][0]["format"] = format_name
    if format_name == "hdr":
        data["variants"][0]["sample_type"] = "float32"
    else:
        data["variants"][0]["sample_type"] = "uint8"
    with pytest.raises(ValidationError, match="EXR"):
        ImageSpecification.model_validate(data)


@pytest.mark.parametrize("field", ["dimensions", "display_window"])
@pytest.mark.parametrize("dimension", ["width", "height"])
@pytest.mark.parametrize("size", [0, -1])
def test_window_sizes_must_be_positive(field: str, dimension: str, size: int) -> None:
    """Origins may be negative; sizes may not."""
    data = window_specification_data()
    data["source"][field][dimension] = size
    with pytest.raises(ValidationError, match="greater than 0"):
        ImageSpecification.model_validate(data)


@pytest.mark.parametrize("field", ["data_window", "display_window"])
def test_window_alone_is_an_expectation(field: str) -> None:
    """Storage window assertions count as nonempty expectations."""
    window = {"x": -3, "y": 5, "width": 4, "height": 2}
    expected = Expectations.model_validate({field: window})
    assert getattr(expected, field).model_dump() == window


@pytest.mark.parametrize("explicit_origin", [False, True])
def test_default_windows_roundtrip(tmp_path: Path, *, explicit_origin: bool) -> None:
    """Omitted windows retain the data extent, including an offset origin."""
    data = window_specification_data()
    del data["source"]["display_window"]
    if not explicit_origin:
        del data["source"]["data_origin"]
        origin_x, origin_y = 0, 0
    else:
        origin_x, origin_y = -3, 5
    specification = ImageSpecification.model_validate(data)
    path = write_variant(specification, specification.variants[0], tmp_path)
    native = read_image(path).nativespec()
    assert (native.x, native.y, native.width, native.height) == (origin_x, origin_y, 4, 2)
    assert (native.full_x, native.full_y, native.full_width, native.full_height) == (origin_x, origin_y, 4, 2)


@pytest.mark.parametrize("field", ["data_window", "display_window"])
@pytest.mark.parametrize("dimension", ["width", "height"])
def test_expected_window_size_must_be_positive(field: str, dimension: str) -> None:
    """Storage assertions cannot request an empty window."""
    window = {"x": -3, "y": 5, "width": 4, "height": 2}
    window[dimension] = 0
    with pytest.raises(ValidationError, match="greater than 0"):
        Expectations.model_validate({field: window})


@pytest.mark.parametrize("sample_type", ["float16", "float32"])
@pytest.mark.parametrize("origin", [{"x": -3, "y": 5}, {"x": 3, "y": -5}])
def test_window_roundtrip(sample_type: str, origin: dict, tmp_path: Path) -> None:
    """HALF and FLOAT preserve data and display windows independently."""
    data = window_specification_data()
    data["source"]["data_origin"] = origin
    data["variants"][0]["sample_type"] = sample_type
    data["variants"][0]["expect"] = {
        "dimensions": {"width": 4, "height": 2},
        "data_window": origin | {"width": 4, "height": 2},
        "display_window": data["source"]["display_window"],
        "sample_type": sample_type,
        "pixels": {"checks": [{"kind": "all_pixels", "colour": [0.5], "max_absolute_error": 0}]},
    }
    specification = ImageSpecification.model_validate(data)
    path = write_variant(specification, specification.variants[0], tmp_path)
    image = read_image(path)
    native = image.nativespec()
    assert (native.x, native.y, native.width, native.height) == (origin["x"], origin["y"], 4, 2)
    assert (native.full_x, native.full_y, native.full_width, native.full_height) == (-5, -2, 12, 10)
    assert image.getpixel(origin["x"], origin["y"]) == (0.5,)


@pytest.mark.parametrize(
    ("pattern", "rows"),
    [
        ({"pattern": "solid", "colour": [0.5]}, [[0.5] * 4, [0.5] * 4]),
        ({"pattern": "gradient", "axis": "x", "start": [0], "end": [1]}, [[0, 1 / 3, 2 / 3, 1]] * 2),
        ({"pattern": "gradient", "axis": "y", "start": [0], "end": [1]}, [[0] * 4, [1] * 4]),
        ({"pattern": "colour_bars", "axis": "x", "colours": [[0], [1]]}, [[0, 0, 1, 1]] * 2),
        ({"pattern": "colour_bars", "axis": "y", "colours": [[0], [1]]}, [[0] * 4, [1] * 4]),
        (
            {"pattern": "checkerboard", "cell_dimensions": {"width": 1, "height": 1}, "colours": [[0], [1]]},
            [[0, 1, 0, 1], [1, 0, 1, 0]],
        ),
        ({"pattern": "quadrants", "colours": [[0], [1], [0.25], [0.75]]}, [[0, 0, 1, 1], [0.25, 0.25, 0.75, 0.75]]),
    ],
)
def test_patterns_use_local_coordinates(pattern: dict, rows: list[list[float]], tmp_path: Path) -> None:
    """Every procedural ROI translates into the offset data window."""
    data = window_specification_data()
    data["source"]["pixels"] = pattern
    specification = ImageSpecification.model_validate(data)
    path = write_variant(specification, specification.variants[0], tmp_path)
    image = read_image(path)
    for y, row in enumerate(rows):
        actual = [image.getpixel(x - 3, y + 5)[0] for x in range(4)]
        assert actual == pytest.approx(row, abs=1e-7)


def write_independent_exr(path: Path) -> None:
    """Write without fixture generation so assertion defects cannot cancel out."""
    spec = oiio.ImageSpec(4, 2, 1, oiio.FLOAT)
    spec.x, spec.y = -3, 5
    spec.full_x, spec.full_y = -5, -2
    spec.full_width, spec.full_height = 12, 10
    image = oiio.ImageBuf(spec)
    filled = oiio.ImageBufAlgo.fill(image, (0.25,))
    assert filled
    image.setpixel(-1, 6, (0.75,))
    image.setpixel(0, 6, (0.75,))
    written = image.write(str(path))
    assert written


@pytest.mark.parametrize("field", ["data_window", "display_window"])
@pytest.mark.parametrize("component", ["x", "y", "width", "height"])
def test_independent_window_assertions_reject_mismatch(field: str, component: str, tmp_path: Path) -> None:
    """Native origin and dimensions are checked, not just decoded dimensions."""
    path = tmp_path / "independent.exr"
    write_independent_exr(path)
    values = {
        "data_window": {"x": -3, "y": 5, "width": 4, "height": 2},
        "display_window": {"x": -5, "y": -2, "width": 12, "height": 10},
    }
    expected = Expectations.model_validate(values)
    assert_image(path, expected, label="correct")
    values[field][component] += 1
    expected = Expectations.model_validate(values)
    with pytest.raises(AssertionError, match=field):
        assert_image(path, expected, label="wrong")


@pytest.mark.parametrize("kind", ["sample", "region", "all_pixels"])
def test_independent_pixel_checks_are_local(kind: str, tmp_path: Path) -> None:
    """Selection and diagnostics use local coordinates even at negative origins."""
    path = tmp_path / "independent.exr"
    write_independent_exr(path)
    check = {"kind": kind, "colour": [0.75], "max_absolute_error": 0}
    if kind == "sample":
        check["position"] = {"x": 2, "y": 1}
    elif kind == "region":
        check["region"] = {"x": 2, "y": 1, "width": 2, "height": 1}
    else:
        check["colour"] = [0.25]
    expected = Expectations.model_validate({"pixels": {"checks": [check]}})
    if kind == "all_pixels":
        with pytest.raises(AssertionError, match=r"pixel \(2, 1\)"):
            assert_image(path, expected, label="all")
        return
    assert_image(path, expected, label="selected")
    check["colour"] = [0.5]
    expected = Expectations.model_validate({"pixels": {"checks": [check]}})
    with pytest.raises(AssertionError, match=r"pixel \(2, 1\)"):
        assert_image(path, expected, label="wrong")
    if kind == "sample":
        check["position"]["x"] = 4
    else:
        check["region"]["x"] = 4
    expected = Expectations.model_validate({"pixels": {"checks": [check]}})
    with pytest.raises(AssertionError, match="outside image width"):
        assert_image(path, expected, label="outside")
