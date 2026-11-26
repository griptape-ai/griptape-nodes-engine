"""Shared implementation for the built-in Pillow image backend."""

# Maps PIL image mode to (channels, color_space) for metadata reporting
_MODE_INFO: dict[str, tuple[int, str]] = {
    "L": (1, "Grayscale"),
    "P": (1, "Palette"),
    "RGB": (3, "RGB"),
    "RGBA": (4, "RGBA"),
    "CMYK": (4, "CMYK"),
    "YCbCr": (3, "YCbCr"),
    "LAB": (3, "LAB"),
    "HSV": (3, "HSV"),
    "I": (1, "Grayscale"),
    "F": (1, "Grayscale"),
    "LA": (2, "Grayscale+Alpha"),
    "RGBa": (4, "RGBA"),  # spellchecker:disable-line
    "RGBX": (4, "RGB"),
}


def get_supported_source_formats() -> set[str]:
    """Return a fresh set of extensions supported by the Pillow image backend."""
    return {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}


def get_mode_info(mode: str) -> tuple[int, str]:
    """Return the channel count and colour space for a Pillow mode."""
    return _MODE_INFO.get(mode, (3, mode))
