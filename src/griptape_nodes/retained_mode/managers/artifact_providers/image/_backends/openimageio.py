"""Source formats supported by the engine's OpenImageIO backend."""

from functools import cache

import OpenImageIO as oiio

SUPPORTED_SOURCE_PLUGINS = frozenset(
    {
        "bmp",
        "cineon",
        "dds",
        "dpx",
        "fits",
        "gif",
        "hdr",
        "ico",
        "jpeg",
        "jpeg2000",
        "jpegxl",
        "openexr",
        "png",
        "pnm",
        "psd",
        "rla",
        "sgi",
        "softimage",
        "targa",
        "tiff",
        "webp",
    }
)


def get_supported_source_formats() -> set[str]:
    """Return a fresh set of extensions for installed, allowlisted readers."""
    return set(_discover_source_formats())


@cache
def _discover_source_formats() -> frozenset[str]:
    extensions: set[str] = set()
    for plugin in oiio.get_string_attribute("extension_list").split(";"):
        format_name, _, format_extensions = plugin.partition(":")
        if format_name not in SUPPORTED_SOURCE_PLUGINS:
            continue
        if oiio.ImageInput.create(format_name) is None:
            oiio.geterror()
            continue
        extensions.update(format_extensions.split(","))
    return frozenset(extensions)
