"""Shared implementation for the built-in OpenImageIO image backend."""

from functools import cache

import OpenImageIO as oiio

from griptape_nodes.files.path_utils import canonicalize_for_io
from griptape_nodes.retained_mode.managers.artifact_providers.image.base_image_artifact_provider import (
    ImageArtifactMetadata,
)

_FORMAT_NAMES = {"openexr": "EXR", "targa": "TGA"}

EXIF_ORIENTATION_NORMAL = 1
EXIF_ORIENTATION_FLIP_HORIZONTAL = 2
EXIF_ORIENTATION_ROTATE_180 = 3
EXIF_ORIENTATION_FLIP_VERTICAL = 4
EXIF_ORIENTATION_TRANSPOSE = 5
EXIF_ORIENTATION_ROTATE_90_CLOCKWISE = 6
EXIF_ORIENTATION_TRANSVERSE = 7
EXIF_ORIENTATION_ROTATE_90_COUNTERCLOCKWISE = 8

AXIS_SWAPPING_ORIENTATIONS = (
    EXIF_ORIENTATION_TRANSPOSE,
    EXIF_ORIENTATION_ROTATE_90_CLOCKWISE,
    EXIF_ORIENTATION_TRANSVERSE,
    EXIF_ORIENTATION_ROTATE_90_COUNTERCLOCKWISE,
)


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


def orientation_swaps_axes(spec: oiio.ImageSpec) -> bool:
    """Return whether EXIF orientation swaps the width and height."""
    return spec.get_int_attribute("Orientation", EXIF_ORIENTATION_NORMAL) in AXIS_SWAPPING_ORIENTATIONS


def read_artifact_metadata(source_path: str) -> ImageArtifactMetadata | None:
    """Read first-subimage metadata without decoding pixels."""
    try:
        path = canonicalize_for_io(source_path)
        file_size = path.stat().st_size
    except (OSError, ValueError):
        return None

    reader = oiio.ImageInput.open(str(path))
    if reader is None:
        oiio.geterror()
        return None

    try:
        spec = reader.spec()
        plugin = reader.format_name()
        width, height = spec.width, spec.height
        if plugin == "openexr":
            width, height = spec.full_width, spec.full_height
        if orientation_swaps_axes(spec):
            width, height = height, width
        return ImageArtifactMetadata(
            width=width,
            height=height,
            format=_FORMAT_NAMES.get(plugin, plugin.upper()),
            channels=spec.nchannels,
            color_space=spec.get_string_attribute("oiio:ColorSpace") or "Unknown",
            file_size=file_size,
        )
    finally:
        reader.close()
