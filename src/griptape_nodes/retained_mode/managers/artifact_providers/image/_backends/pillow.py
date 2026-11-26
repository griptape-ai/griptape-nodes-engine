"""Source formats supported by the built-in Pillow image backend."""


def get_supported_source_formats() -> set[str]:
    """Return a fresh set of extensions supported by the Pillow image backend."""
    return {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}
