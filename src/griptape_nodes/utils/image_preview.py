"""Image preview utilities for generating thumbnails and inspecting headers."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline, ImageFormat, ImageMetadata

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("griptape_nodes")

_CONVERT_TO_RGB_FOR_LOSSY = frozenset({"RGBA", "Grayscale+Alpha"})
_LOSSY_FORMATS = frozenset({ImageFormat.WEBP, ImageFormat.JPEG})
_NO_ALPHA_FORMATS = frozenset({ImageFormat.JPEG, ImageFormat.MPO})


def create_image_preview(
    image_path: Path, max_width: int = 512, max_height: int = 512, quality: int = 85, image_format: str = "WEBP"
) -> str | None:
    """Create a small preview image from a file path as a base64 data URL, or None on failure."""
    # TODO: Add SVG support using cairosvg or similar library to rasterize SVG files: https://github.com/griptape-ai/griptape-nodes-engine/issues/3721
    if image_path.suffix.lower() == ".svg":
        logger.debug("SVG file detected, cannot create preview (vector graphics unsupported): %s", image_path)
        return None

    try:
        with GriptapeImage.open(image_path) as source:
            data_url = _render_preview_data_url(source, max_width, max_height, quality, image_format)
            logger.debug(
                "Created preview for %s: %dx%d -> %d chars", image_path, source.width, source.height, len(data_url)
            )
            return data_url
    except Exception as e:
        logger.warning("Failed to create preview for %s: %s", image_path, e)
        return None


def create_image_preview_from_bytes(
    image_bytes: bytes, max_width: int = 512, max_height: int = 512, quality: int = 85, image_format: str = "WEBP"
) -> str | None:
    """Create a small preview image from raw image bytes as a base64 data URL, or None on failure."""
    content_start = image_bytes[:100].decode("utf-8", errors="ignore").lower()
    if "<svg" in content_start:
        logger.debug("SVG content detected, cannot create preview (vector graphics unsupported)")
        return None

    try:
        with GriptapeImage.open(image_bytes) as source:
            data_url = _render_preview_data_url(source, max_width, max_height, quality, image_format)
            logger.debug("Created preview from bytes: %d bytes -> %d chars", len(image_bytes), len(data_url))
            return data_url
    except Exception as e:
        logger.warning("Failed to create preview from bytes: %s", e)
        return None


def get_image_info(image_path: Path) -> dict[str, object] | None:
    """Return header dimensions (after EXIF orientation), format, color_space, and file size, or None."""
    try:
        with GriptapeImage.open(image_path) as img:
            width, height = img.oriented_size
            return {
                "width": width,
                "height": height,
                "format": img.format,
                "color_space": img.color_space,
                "size_bytes": image_path.stat().st_size,
            }
    except Exception as e:
        logger.warning("Failed to get image info for %s: %s", image_path, e)
        return None


def _render_preview_data_url(
    source: GriptapeImage, max_width: int, max_height: int, quality: int, image_format: str
) -> str:
    target = ImageFormat.from_name(image_format)
    if target is None:
        msg = f"Attempted to create an image preview. Failed because {image_format} is not a supported image format."
        raise ValueError(msg)

    pipeline = GriptapeImagePipeline(source).exif_transpose()
    if (
        target in _LOSSY_FORMATS
        and source.format not in _NO_ALPHA_FORMATS
        and source.color_space in _CONVERT_TO_RGB_FOR_LOSSY
    ):
        pipeline.to_rgb()
    preview = pipeline.thumbnail(max_width, max_height).end()
    preview_bytes = preview.encode(
        target,
        quality=quality,
        optimize=True,
        metadata=ImageMetadata(icc_profile=preview.metadata.icc_profile),
    )

    base64_data = base64.b64encode(preview_bytes).decode("utf-8")
    mime_type = "image/jpeg" if target == ImageFormat.JPEG else f"image/{target.extension}"
    return f"data:{mime_type};base64,{base64_data}"
