"""Loader wiring, declarative schema and cross-field constraints."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml.constructor import DuplicateKeyError

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    Specification,
    expand_cases,
    load_specifications,
    load_thumbnail_specifications,
)


def specification_data() -> dict:
    """Provide one image with an encoded input and thumbnail contract."""
    return {
        "schema_version": 1,
        "images": [
            {
                "id": "red",
                "source": {
                    "dimensions": {"width": 8, "height": 4},
                    "channels": ["R", "G", "B"],
                    "pixels": {"pattern": "solid", "colour": [1, 0, 0]},
                },
                "variants": [{"id": "png8", "format": "png", "sample_type": "uint8"}],
                "cases": [
                    {
                        "id": "small",
                        "operation": "generate_thumbnail",
                        "inputs": ["png8"],
                        "outputs": [{"format": "png", "expect": {"dimensions": {"width": 4, "height": 2}}}],
                    }
                ],
            }
        ],
    }


def test_schema_declares_discriminated_models() -> None:
    """Pydantic's schema describes required fields and legal format depths."""
    schema = Specification.model_json_schema()
    definitions = schema["$defs"]
    source = definitions["Source"]["properties"]["pixels"]
    checks = definitions["PixelChecks"]["properties"]["checks"]["items"]
    variants = definitions["ImageSpecification"]["properties"]["variants"]["items"]
    assert source["discriminator"]["propertyName"] == "pattern"
    assert checks["discriminator"]["propertyName"] == "kind"
    assert variants["discriminator"]["propertyName"] == "format"
    solid = definitions["SolidPattern"]
    assert "colour" in solid["required"]
    assert "axis" not in solid["properties"]
    jpeg = definitions["JpegVariant"]["properties"]["sample_type"]
    assert jpeg["const"] == "uint8"


def test_repository_specification_expands_unchanged() -> None:
    """The split specification preserves the original case count and order."""
    specification = load_thumbnail_specifications()
    cases = expand_cases(specification)
    expected_case_count = 303
    assert len(cases) == expected_case_count
    assert cases == expand_cases(specification)
    assert cases[0].id == "square/png8/default/png"
    assert cases[284].id == "gradient/png16/resampling/png"
    assert cases[285].id == "checkerboard/png8/resampling/png"
    assert cases[294].id == "window_partial_overlap/exr_half/display_frame/png"
    assert cases[-1].id == "alpha_edge_associated/exr_half/resize/webp"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("source", "channels"), ["R", "R", "B"]),
        (("source", "pixels", "colour"), [1]),
        (("source", "alpha_association"), "straight"),
        (("cases", 0, "inputs"), ["missing"]),
        (("cases", 0, "inputs"), ["png8", "png8"]),
        (("variants", 0, "sample_type"), "uint1"),
        (("cases", 0, "outputs", 0, "expect"), {}),
        (("cases", 0, "outputs", 0, "expect", "encoding"), {}),
        (("cases", 0, "outputs", 0, "expect", "metadata"), {"present": ["key"], "absent": ["key"]}),
    ],
)
def test_cross_field_constraints(path: tuple, value: object) -> None:
    """Retain checks for rules that require relationships between fields."""
    data = specification_data()
    parent = data["images"][0]
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValidationError):
        Specification.model_validate(data)


@pytest.mark.parametrize("collection", ["images", "variants", "cases", "outputs"])
def test_duplicate_identifiers(collection: str) -> None:
    """Duplicate identifiers cannot produce ambiguous fixture or test names."""
    data = specification_data()
    collections = {
        "images": data["images"],
        "variants": data["images"][0]["variants"],
        "cases": data["images"][0]["cases"],
        "outputs": data["images"][0]["cases"][0]["outputs"],
    }
    values = collections[collection]
    values.append(deepcopy(values[0]))
    with pytest.raises(ValidationError):
        Specification.model_validate(data)


def test_loader_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    """The YAML reader refuses duplicate keys before model validation."""
    path = tmp_path / "duplicate.yaml"
    path.write_text("schema_version: 1\nschema_version: 1\nimages: []\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError):
        load_specifications(path)


def test_loader_validates_yaml(tmp_path: Path) -> None:
    """Loaded YAML passes through the Pydantic root model."""
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 2\nimages: []\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_specifications(path)
