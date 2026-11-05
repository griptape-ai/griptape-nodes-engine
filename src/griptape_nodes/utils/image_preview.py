"""Image preview utilities for generating thumbnails and previews."""

import base64
from pathlib import Path

from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline, ImageFormat, open_image
from griptape_nodes.retained_mode.griptape_nodes import logger

# Color models that JPEG/WEBP previews are flattened out of before encoding
_CONVERT_TO_RGB_FOR_LOSSY = {"RGBA", "Grayscale+Alpha", "Palette"}
_LOSSY_FORMATS = {ImageFormat.WEBP, ImageFormat.JPEG}
# Sources that never hold alpha or a palette, so their color model needn't be read (which decodes
# the full image on some backends, before a reduced-size decode could apply)
_NO_ALPHA_FORMATS = {ImageFormat.JPEG, ImageFormat.MPO}


def create_image_preview(
    image_path: Path, max_width: int = 512, max_height: int = 512, quality: int = 85, image_format: str = "WEBP"
) -> str | None:
    """Create a small preview image from a file path.

    Args:
        image_path: Path to the image file
        max_width: Maximum width for the preview
        max_height: Maximum height for the preview
        quality: WebP quality (1-100)
        image_format: Output format (WEBP, JPEG, PNG, etc.)

    Returns:
        Base64 encoded data URL of the preview, or None if failed
    """
    # Check if it's an SVG file - PIL cannot open SVG files
    # TODO: Add SVG support using cairosvg or similar library to rasterize SVG files: https://github.com/griptape-ai/griptape-nodes-engine/issues/3721
    # before creating previews
    if image_path.suffix.lower() == ".svg":
        logger.debug(f"SVG file detected, cannot create preview with PIL (vector graphics not supported): {image_path}")
        return None

    try:
        with open_image(image_path) as source:
            data_url = _render_preview_data_url(source, max_width, max_height, quality, image_format)
            logger.debug(
                f"Created preview for {image_path}: {source.width}x{source.height} -> {len(data_url)} characters"
            )
            return data_url

    except Exception as e:
        # Check if error is due to SVG format
        if "cannot identify image file" in str(e).lower() and image_path.suffix.lower() == ".svg":
            logger.debug(
                f"SVG file detected, cannot create preview with PIL (vector graphics not supported): {image_path}"
            )
            return None
        logger.warning(f"Failed to create preview for {image_path}: {e}")
        return None


def create_image_preview_from_bytes(
    image_bytes: bytes, max_width: int = 512, max_height: int = 512, quality: int = 85, image_format: str = "WEBP"
) -> str | None:
    """Create a small preview image from bytes.

    Args:
        image_bytes: Raw image bytes
        max_width: Maximum width for the preview
        max_height: Maximum height for the preview
        quality: WebP quality (1-100)
        image_format: Output format (WEBP, JPEG, PNG, etc.)

    Returns:
        Base64 encoded data URL of the preview, or None if failed
    """
    try:
        # Check if content might be SVG by looking at first few bytes
        # TODO: Add SVG support using cairosvg or similar library to rasterize SVG files: https://github.com/griptape-ai/griptape-nodes-engine/issues/3721
        # before creating previews
        content_start = image_bytes[:100].decode("utf-8", errors="ignore").lower()
        if "<svg" in content_start:
            logger.debug("SVG file detected, cannot create preview with PIL (vector graphics not supported)")
            return None

        with open_image(image_bytes) as source:
            data_url = _render_preview_data_url(source, max_width, max_height, quality, image_format)
            logger.debug(f"Created preview from bytes: {len(image_bytes)} bytes -> {len(data_url)} characters")
            return data_url

    except Exception as e:
        # Check if error is due to SVG format
        if "cannot identify image file" in str(e).lower():
            content_start = image_bytes[:100].decode("utf-8", errors="ignore").lower()
            if "<svg" in content_start:
                logger.debug("SVG file detected, cannot create preview with PIL (vector graphics not supported)")
                return None
        logger.warning(f"Failed to create preview from bytes: {e}")
        return None


def get_image_info(image_path: Path) -> dict | None:
    """Get basic information about an image file.

    Args:
        image_path: Path to the image file

    Returns:
        Dictionary with image info (width, height, format, color_space), or None if failed
    """
    try:
        with open_image(image_path) as img:
            # Report the dimensions the image displays at, without decoding and rotating it
            width = img.width
            height = img.height
            exif = img.metadata.exif
            if exif is not None and exif.swaps_dimensions:
                width, height = height, width
            return {
                "width": width,
                "height": height,
                "format": img.format,
                "color_space": img.color_space,
                "size_bytes": image_path.stat().st_size,
            }
    except Exception as e:
        logger.warning(f"Failed to get image info for {image_path}: {e}")
        return None


def _render_preview_data_url(
    source: GriptapeImage, max_width: int, max_height: int, quality: int, image_format: str
) -> str:
    """Shrink ``source`` to fit the bounds and return it encoded as a base64 data URL.

    Raises:
        ValueError: If ``image_format`` is not a known image format
    """
    target = ImageFormat.from_name(image_format)
    if target is None:
        msg = f"Attempted to create an image preview. Failed because {image_format} is not a known image format."
        raise ValueError(msg)
    with GriptapeImagePipeline(source) as pipeline:
        # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly
        pipeline.exif_transpose()
        # Convert to RGB if necessary (for WebP/JPEG output)
        if (
            target in _LOSSY_FORMATS
            and source.format not in _NO_ALPHA_FORMATS
            and source.color_space in _CONVERT_TO_RGB_FOR_LOSSY
        ):
            pipeline.to_rgb()
        pipeline.thumbnail(max_width, max_height)
        with pipeline.end() as preview:
            preview_bytes = preview.encode(target, quality=quality, optimize=True)

    base64_data = base64.b64encode(preview_bytes).decode("utf-8")
    mime_type = f"image/{image_format.lower()}"
    return f"data:{mime_type};base64,{base64_data}"
