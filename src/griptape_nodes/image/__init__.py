"""Image decoding, transforms, and encoding behind one interface with swappable backends.

``open_image`` returns a ``GriptapeImage`` from the backend named by the ``GTN_IMAGE_BACKEND``
environment variable: ``pillow`` (default), ``opencv``, or ``openimageio``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from griptape_nodes.image.griptape_image import GriptapeImage, normalize_format_name
from griptape_nodes.image.opencv_image import OpenCVImage
from griptape_nodes.image.openimageio_image import OpenImageIOImage
from griptape_nodes.image.pillow_image import PillowImage

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("griptape_nodes")

IMAGE_BACKEND_ENV_VAR = "GTN_IMAGE_BACKEND"
PILLOW_BACKEND = "pillow"
OPENCV_BACKEND = "opencv"
OPENIMAGEIO_BACKEND = "openimageio"

_BACKENDS: dict[str, type[GriptapeImage]] = {
    PILLOW_BACKEND: PillowImage,
    OPENCV_BACKEND: OpenCVImage,
    OPENIMAGEIO_BACKEND: OpenImageIOImage,
}


def image_backend_name() -> str:
    """Return the backend ``open_image`` uses, falling back to Pillow for an unknown value."""
    requested = os.getenv(IMAGE_BACKEND_ENV_VAR, PILLOW_BACKEND).strip().lower()
    if requested not in _BACKENDS:
        logger.warning(
            "Ignoring %s=%r: expected one of %s. Using %s.",
            IMAGE_BACKEND_ENV_VAR,
            requested,
            ", ".join(sorted(_BACKENDS)),
            PILLOW_BACKEND,
        )
        return PILLOW_BACKEND
    return requested


def open_image(source: bytes | Path) -> GriptapeImage:
    """Open an image from encoded bytes or a file path with the configured backend.

    Raises:
        OSError: If the file cannot be read
        ValueError: If the data is not an image the backend can read (OpenCV, OpenImageIO; a damaged image fails on first pixel access)
        PIL.UnidentifiedImageError: If the data is not an image the backend can read (Pillow)
    """
    return _BACKENDS[image_backend_name()].open(source)


__all__ = [
    "IMAGE_BACKEND_ENV_VAR",
    "OPENCV_BACKEND",
    "OPENIMAGEIO_BACKEND",
    "PILLOW_BACKEND",
    "GriptapeImage",
    "OpenCVImage",
    "OpenImageIOImage",
    "PillowImage",
    "image_backend_name",
    "normalize_format_name",
    "open_image",
]
