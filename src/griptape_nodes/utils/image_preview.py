"""Image preview utilities for generating thumbnails and previews."""

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps

from griptape_nodes.retained_mode.griptape_nodes import logger


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
        # Open and resize the image
        with Image.open(image_path) as raw_img:
            # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly
            img = ImageOps.exif_transpose(raw_img)
            # Convert to RGB if necessary (for WebP/JPEG output)
            if image_format.upper() in ("WEBP", "JPEG") and img.mode in ("RGBA", "LA", "P"):
                converted_img = img.convert("RGB")
            else:
                converted_img = img

            # Calculate new size maintaining aspect ratio
            converted_img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

            # Save to bytes buffer
            buffer = io.BytesIO()
            converted_img.save(buffer, format=image_format, quality=quality, optimize=True)
            buffer.seek(0)

            # Convert to base64
            image_bytes = buffer.getvalue()
            base64_data = base64.b64encode(image_bytes).decode("utf-8")

            # Create data URL
            mime_type = f"image/{image_format.lower()}"
            data_url = f"data:{mime_type};base64,{base64_data}"

            logger.debug(f"Created preview for {image_path}: {img.size} -> {len(image_bytes)} bytes")
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

        # Open image from bytes
        with Image.open(io.BytesIO(image_bytes)) as raw_img:
            # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly
            img = ImageOps.exif_transpose(raw_img)
            # Convert to RGB if necessary (for WebP/JPEG output)
            if image_format.upper() in ("WEBP", "JPEG") and img.mode in ("RGBA", "LA", "P"):
                converted_img = img.convert("RGB")
            else:
                converted_img = img

            # Calculate new size maintaining aspect ratio
            converted_img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

            # Save to bytes buffer
            buffer = io.BytesIO()
            converted_img.save(buffer, format=image_format, quality=quality, optimize=True)
            buffer.seek(0)

            # Convert to base64
            preview_bytes = buffer.getvalue()
            base64_data = base64.b64encode(preview_bytes).decode("utf-8")

            # Create data URL
            mime_type = f"image/{image_format.lower()}"
            data_url = f"data:{mime_type};base64,{base64_data}"

            logger.debug(f"Created preview from bytes: {len(image_bytes)} -> {len(preview_bytes)} bytes")
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
        Dictionary with image info (width, height, format, mode), or None if failed
    """
    try:
        with Image.open(image_path) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            return {
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "mode": img.mode,
                "size_bytes": image_path.stat().st_size,
            }
    except Exception as e:
        logger.warning(f"Failed to get image info for {image_path}: {e}")
        return None
