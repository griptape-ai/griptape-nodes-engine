"""Procedural inputs have independently specified pixels and storage."""

from pathlib import Path

import pytest
from PIL import Image

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_generation import (
    read_image,
    write_variant,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    ImageSpecification,
)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ({"pattern": "solid", "colour": [1]}, [1, 1, 1, 1]),
        ({"pattern": "gradient", "axis": "x", "start": [0], "end": [1]}, [0, 1 / 3, 2 / 3, 1]),
        ({"pattern": "colour_bars", "axis": "x", "colours": [[0], [1]]}, [0, 0, 1, 1]),
        (
            {"pattern": "checkerboard", "cell_dimensions": {"width": 1, "height": 1}, "colours": [[0], [1]]},
            [0, 1, 0, 1],
        ),
        ({"pattern": "quadrants", "colours": [[0], [1], [0.25], [0.75]]}, [0, 0, 1, 1]),
    ],
)
def test_pattern_pixels(pattern: dict, expected: list[float], tmp_path: Path) -> None:
    """Test pattern pixels."""
    specification = ImageSpecification.model_validate(
        {
            "id": "pattern",
            "source": {"dimensions": {"width": 4, "height": 2}, "channels": ["Y"], "pixels": pattern},
            "variants": [{"id": "png16", "format": "png", "sample_type": "uint16"}],
        }
    )
    path = write_variant(specification, specification.variants[0], tmp_path)
    image = read_image(path)
    pixels = [image.getpixel(x, 0)[0] for x in range(4)]
    assert pixels == pytest.approx(expected, abs=1 / 65535)
    if pattern["pattern"] == "quadrants":
        bottom_left = image.getpixel(0, 1)[0]
        bottom_right = image.getpixel(3, 1)[0]
        assert bottom_left == pytest.approx(0.25, abs=1 / 65535)
        assert bottom_right == pytest.approx(0.75, abs=1 / 65535)


def test_uint16_round_half_up_and_metadata_override(tmp_path: Path) -> None:
    """Test uint16 round half up and metadata override."""
    specification = ImageSpecification.model_validate(
        {
            "id": "metadata",
            "source": {
                "dimensions": {"width": 2, "height": 2},
                "channels": ["R", "G", "B"],
                "metadata": {"gtn_workflow_name": "original"},
                "pixels": {"pattern": "solid", "colour": [0.5, 0.2501, 0.7501]},
            },
            "variants": [
                {
                    "id": "png16",
                    "format": "png",
                    "sample_type": "uint16",
                    "metadata": {"gtn_workflow_name": "test_workflow"},
                    "expect": {"metadata": {"equals": {"gtn_workflow_name": "test_workflow"}}},
                }
            ],
        }
    )
    path = write_variant(specification, specification.variants[0], tmp_path)
    image = read_image(path)
    assert str(image.nativespec().format) == "uint16"
    pixel = image.getpixel(0, 0)[0]
    assert pixel == pytest.approx(32768 / 65535, abs=1e-7)
    assert image.nativespec().get_string_attribute("gtn_workflow_name") == "test_workflow"


def test_one_bit_source_is_stored_as_one_bit(tmp_path: Path) -> None:
    """Test one bit source is stored as one bit."""
    specification = ImageSpecification.model_validate(
        {
            "id": "one_bit",
            "source": {
                "dimensions": {"width": 2, "height": 2},
                "channels": ["Y"],
                "pixels": {"pattern": "solid", "colour": [1]},
            },
            "variants": [{"id": "png1", "format": "png", "sample_type": "uint1"}],
        }
    )
    path = write_variant(specification, specification.variants[0], tmp_path)
    with Image.open(path) as image:
        mode = image.mode
    assert mode == "1"
