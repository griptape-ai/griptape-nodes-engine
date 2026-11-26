"""Tests for the Pillow image backend."""

import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import pillow


@pytest.mark.parametrize(
    ("mode", "expected_channels", "expected_colour_space"),
    [
        ("L", 1, "Grayscale"),
        ("P", 1, "Palette"),
        ("RGB", 3, "RGB"),
        ("RGBA", 4, "RGBA"),
        ("CMYK", 4, "CMYK"),
        ("YCbCr", 3, "YCbCr"),
        ("LAB", 3, "LAB"),
        ("HSV", 3, "HSV"),
        ("I", 1, "Grayscale"),
        ("F", 1, "Grayscale"),
        ("LA", 2, "Grayscale+Alpha"),
        ("RGBa", 4, "RGBA"),  # spellchecker:disable-line
        ("RGBX", 4, "RGB"),
    ],
    ids=["L", "P", "RGB", "RGBA", "CMYK", "YCbCr", "LAB", "HSV", "I", "F", "LA", "premultiplied-alpha", "RGBX"],
)
def test_get_mode_info_returns_expected_metadata(mode: str, expected_channels: int, expected_colour_space: str) -> None:
    """Known Pillow modes report their expected channel count and colour space."""
    assert pillow.get_mode_info(mode) == (expected_channels, expected_colour_space)


def test_get_mode_info_defaults_to_three_channels_and_preserves_unknown_mode() -> None:
    """Unknown modes retain their name in the reported colour space."""
    assert pillow.get_mode_info("unknown-mode") == (3, "unknown-mode")
