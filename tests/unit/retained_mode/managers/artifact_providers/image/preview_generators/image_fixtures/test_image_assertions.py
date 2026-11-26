"""Declarative checks reject independently constructed defects."""

from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_assertions import (
    assert_image,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    Expectations,
)


@pytest.mark.parametrize("field", ["dimensions", "sample_type", "channels", "equals", "present", "absent", "pixels"])
def test_checks_accept_expected_and_reject_incorrect_values(field: str, tmp_path: Path) -> None:
    """Test checks accept expected and reject incorrect values."""
    path = tmp_path / "red.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("gtn_workflow_name", "test_workflow")
    with Image.new("RGB", (2, 2), (255, 0, 0)) as image:
        image.save(path, pnginfo=metadata)
    correct = {
        "dimensions": {"width": 2, "height": 2},
        "sample_type": "uint8",
        "channels": ["R", "G", "B"],
        "metadata": {
            "equals": {"gtn_workflow_name": "test_workflow"},
            "present": ["gtn_workflow_name"],
            "absent": ["missing"],
        },
        "pixels": {"checks": [{"kind": "all_pixels", "colour": [1, 0, 0], "max_absolute_error": 0}]},
    }
    assert_image(path, Expectations.model_validate(correct), label="accept")
    if field == "dimensions":
        correct[field]["width"] = 3
    elif field == "sample_type":
        correct[field] = "uint16"
    elif field == "channels":
        correct[field] = ["Y"]
    elif field == "equals":
        correct["metadata"]["equals"]["gtn_workflow_name"] = "wrong"
    elif field == "present":
        correct["metadata"]["present"] = ["another_missing"]
    elif field == "absent":
        correct["metadata"] = {"absent": ["gtn_workflow_name"]}
    else:
        correct[field]["checks"][0]["colour"] = [0, 0, 0]
    with pytest.raises(AssertionError, match="reject"):
        assert_image(path, Expectations.model_validate(correct), label="reject")


@pytest.mark.parametrize("kind", ["all_pixels", "sample", "region"])
def test_pixel_tolerance_and_location(kind: str, tmp_path: Path) -> None:
    """Test pixel tolerance and location."""
    path = tmp_path / "pixel.png"
    with Image.new("RGB", (2, 2), (1, 0, 0)) as image:
        image.save(path)
    check = {"kind": kind, "colour": [0, 0, 0], "max_absolute_error": 1.5 / 255}
    if kind == "sample":
        check["position"] = {"x": 1, "y": 1}
    elif kind == "region":
        check["region"] = {"x": 0, "y": 0, "width": 2, "height": 2}
    assert_image(path, Expectations.model_validate({"pixels": {"checks": [check]}}), label="within")
    check["max_absolute_error"] = 0.5 / 255
    with pytest.raises(AssertionError, match=r"above.*pixel"):
        assert_image(path, Expectations.model_validate({"pixels": {"checks": [check]}}), label="above")


def test_jpeg_encoding_checks(tmp_path: Path) -> None:
    """Test jpeg encoding checks."""
    path = tmp_path / "red.jpg"
    with Image.new("RGB", (8, 8), "red") as image:
        image.save(path, quality=75, subsampling=2)
    expected = Expectations.model_validate({"encoding": {"sampling": 2, "progressive": False}})
    assert_image(path, expected, label="jpeg")
    assert expected.encoding is not None
    expected.encoding.sampling = 0
    with pytest.raises(AssertionError, match=r"jpeg.*sampling"):
        assert_image(path, expected, label="jpeg")
