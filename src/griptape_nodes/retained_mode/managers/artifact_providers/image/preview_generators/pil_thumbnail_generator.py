"""Standard thumbnail generation using Pillow without temporary files."""

from __future__ import annotations

from contextlib import ExitStack
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image, ImageOps, PngImagePlugin

from griptape_nodes.drivers.image_metadata.png_chunks import rewrite_png_text_bytes
from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import pillow
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator_base import (
    BaseThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_parameters import (
    ThumbnailParameters,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
        BaseGeneratorParameters,
    )


class PILThumbnailGenerator(BaseThumbnailGenerator):
    """Fit ordinary image formats within the shared thumbnail dimension limits."""

    def _encode_thumbnail(
        self,
        source_bytes: bytes,
        temp_directory: Path,  # noqa: ARG002 - Shared encoder interface; Pillow works entirely in memory.
        *,
        preserve_text: bool,
        max_source_pixels: int,
    ) -> bytes:
        """Check the source header before decoding, orient, shrink and encode it."""
        source_text: dict[str, str] = {}
        with BytesIO(source_bytes) as source, ExitStack() as images:
            try:
                raw_image = Image.open(source)
            except Image.DecompressionBombError as error:
                raise self._pillow_pixel_limit_error(max_source_pixels) from error
            images.callback(raw_image.close)
            source_pixels = raw_image.width * raw_image.height
            if source_pixels > max_source_pixels:
                msg = (
                    f"Attempted to create a preview for '{self.source_file_location}'. Failed due to the image "
                    f"dimensions {raw_image.width} x {raw_image.height} containing {source_pixels:,} pixels, "
                    f"exceeding max_thumbnail_source_pixels={max_source_pixels:,}. Increase this setting only "
                    "if your machine has enough memory to decode this image. Pillow's independent pixel ceiling "
                    "still applies; larger images require the OpenImageIO backend."
                )
                raise OSError(msg)

            # Loading also reads PNG text stored after the pixel data. Do this only after the budget check.
            try:
                raw_image.load()
            except Image.DecompressionBombError as error:
                raise self._pillow_pixel_limit_error(max_source_pixels) from error
            if (
                preserve_text
                and self.preview_format.lower() == "png"
                and isinstance(raw_image, PngImagePlugin.PngImageFile)
            ):
                source_text = dict(raw_image.text)
            image = images.enter_context(ImageOps.exif_transpose(raw_image))
            image.thumbnail((self.params.max_width, self.params.max_height), Image.Resampling.LANCZOS)

            output_format = self.preview_format.upper()
            if output_format in {"JPG", "JPEG"}:
                output_format = "JPEG"
                if image.mode not in {"RGB", "L"}:
                    image = images.enter_context(image.convert("RGB"))
            elif output_format == "WEBP" and image.mode not in {"RGB", "RGBA"}:
                if "A" in image.getbands() or "transparency" in image.info:
                    image = images.enter_context(image.convert("RGBA"))
                else:
                    image = images.enter_context(image.convert("RGB"))

            with BytesIO() as target:
                image.save(target, format=output_format)
                content = target.getvalue()
        if source_text:
            content = rewrite_png_text_bytes(content, source_text)
        return content

    def _pillow_pixel_limit_error(self, max_source_pixels: int) -> OSError:
        """Explain Pillow's independent safety limit without changing its process-wide policy."""
        ceiling = "its independent pixel ceiling"
        if Image.MAX_IMAGE_PIXELS is not None:
            ceiling = f"its independent ceiling of {2 * Image.MAX_IMAGE_PIXELS:,} pixels"
        message = (
            f"Attempted to create a preview for '{self.source_file_location}'. Failed due to Pillow refusing "
            f"image dimensions above {ceiling}. Raising max_thumbnail_source_pixels={max_source_pixels:,} "
            "cannot bypass this ceiling. To preview larger images, install the OpenImageIO backend and restart "
            "the engine, then increase the setting only if your machine has enough memory."
        )
        return OSError(message)

    @classmethod
    def get_friendly_name(cls) -> str:
        """Use the same public name regardless of the selected image backend."""
        return "Standard Thumbnail Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Return the nine ordinary image extensions supported by the original generator."""
        return pillow.get_supported_source_formats()

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Return the standard preview output formats."""
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Return the shared dimension parameter model."""
        return ThumbnailParameters
