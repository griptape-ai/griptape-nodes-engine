"""Rounded image preview generator using PIL/Pillow."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageOps
from pydantic import PositiveInt  # noqa: TC002 - Runtime validation, not type-only

from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    ReadFileRequest,
    ReadFileResultSuccess,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
    BaseArtifactPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
    BaseGeneratorParameters,
    Field,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class PILRoundedParameters(BaseGeneratorParameters):
    """Parameters for PIL rounded preview generation."""

    max_width: PositiveInt = Field(
        default=1024,
        description="Maximum width in pixels for generated preview (1-8192)",
        editor_schema_type="integer",
        le=8192,
    )

    max_height: PositiveInt = Field(
        default=1024,
        description="Maximum height in pixels for generated preview (1-8192)",
        editor_schema_type="integer",
        le=8192,
    )

    corner_radius_percent: float = Field(
        default=2.0,
        description="Corner radius as percentage of smaller dimension (0-10%)",
        editor_schema_type="number",
        ge=0.0,
        le=10.0,
    )


class PILRoundedPreviewGenerator(BaseArtifactPreviewGenerator):
    """Generate image previews with rounded corners and transparent backgrounds.

    Uses PIL/Pillow to create thumbnails with rounded corners. Corners are transparent
    for PNG/WEBP output formats, or composited onto white background for JPEG.
    """

    def __init__(  # noqa: PLR0913
        self,
        source_file_location: str,
        preview_format: str,
        destination_preview_directory: str,
        destination_preview_file_name: str,
        params: dict[str, Any],
        *,
        engine: Engine | None = None,
    ) -> None:
        """Initialize the rounded preview generator.

        Args:
            source_file_location: Path to the source artifact file
            preview_format: Target format for the preview (e.g., "png", "jpg", "webp")
            destination_preview_directory: Directory where the preview should be saved
            destination_preview_file_name: Filename for the preview
            params: Generator-specific parameters
            engine: The engine whose request bus this generator reads and writes files through

        Raises:
            ValidationError: If parameters are invalid
        """
        super().__init__(
            source_file_location,
            preview_format,
            destination_preview_directory,
            destination_preview_file_name,
            params,
            engine=engine,
        )

        # Validate and convert dict -> Pydantic model
        # Raises ValidationError if invalid
        self.params = PILRoundedParameters.model_validate(params)

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name for this generator.

        Returns:
            The friendly name for this generator
        """
        return "Rounded Image Preview Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process.

        Returns:
            Set of lowercase file extensions WITHOUT leading dots
        """
        return {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces.

        Returns:
            Set of lowercase preview format extensions WITHOUT leading dots
        """
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return PILRoundedParameters

    async def attempt_generate_preview(self) -> str:
        """Attempt to generate preview file with rounded corners.

        Writes file to destination directory using provided filename.

        Returns:
            Filename of generated preview

        Raises:
            FileNotFoundError: If source image cannot be read
            TypeError: If source file is not binary image data
            OSError: If preview write fails
        """
        # Step 1: Read source file
        read_request = ReadFileRequest(
            file_path=self.source_file_location,
            workspace_only=False,
            should_transform_image_content_to_thumbnail=False,
        )
        read_result = await self.engine.ahandle_request(read_request)

        if not isinstance(read_result, ReadFileResultSuccess):
            msg = f"Failed to read source image: {read_result.result_details}"
            raise FileNotFoundError(msg)

        # Step 2: Verify binary data
        image_data = read_result.content
        if isinstance(image_data, str):
            msg = "Source file is text, not binary image data"
            raise TypeError(msg)

        # Step 3: Process image with PIL
        with Image.open(BytesIO(image_data)) as raw_img:
            # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly
            img = ImageOps.exif_transpose(raw_img)
            # Resize to fit max dimensions (preserves aspect ratio)
            # Access validated parameters via self.params - fully type-safe
            img.thumbnail((self.params.max_width, self.params.max_height), Image.Resampling.LANCZOS)

            # Convert to RGBA for alpha channel support
            if img.mode != "RGBA":
                rgba_img = img.convert("RGBA")
            else:
                rgba_img = img

            # Apply rounded corners
            rounded_img = self._apply_rounded_corners(rgba_img)

            # Handle format-specific output
            if self.preview_format.lower() in ("jpg", "jpeg"):
                # JPEG doesn't support transparency - composite onto white background
                white_background = Image.new("RGB", rounded_img.size, (255, 255, 255))
                white_background.paste(rounded_img, (0, 0), rounded_img)
                final_img = white_background
            else:
                # PNG/WEBP support transparency
                final_img = rounded_img

            # Save to buffer
            output_buffer = BytesIO()
            # Normalize format for PIL (JPG -> JPEG)
            pil_format = "JPEG" if self.preview_format.lower() == "jpg" else self.preview_format.upper()
            final_img.save(output_buffer, format=pil_format)
            output_bytes = output_buffer.getvalue()

        # Step 4: Write output file
        destination_path = str(Path(self.destination_preview_directory) / self.destination_preview_file_name)

        write_request = WriteFileRequest(
            file_path=destination_path,
            content=output_bytes,
            create_parents=True,
            existing_file_policy=ExistingFilePolicy.OVERWRITE,
        )
        write_result = await self.engine.ahandle_request(write_request)

        if not isinstance(write_result, WriteFileResultSuccess):
            msg = f"Failed to write preview image: {write_result.result_details}"
            raise OSError(msg)

        return self.destination_preview_file_name

    def _apply_rounded_corners(self, img: Image.Image) -> Image.Image:
        """Apply rounded corners to image using alpha mask.

        Args:
            img: Source image (RGBA mode)

        Returns:
            RGBA image with transparent rounded corners
        """
        # Calculate pixel radius from percentage of smaller dimension
        smaller_dimension = min(img.width, img.height)
        radius_pixels = int((self.params.corner_radius_percent / 100.0) * smaller_dimension)

        # Skip if no rounding requested (0% or rounds to 0 pixels)
        if radius_pixels <= 0:
            return img

        # Clamp radius to prevent over-rounding
        max_radius = smaller_dimension // 2
        effective_radius = min(radius_pixels, max_radius)

        # Create mask for rounded corners
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)], radius=effective_radius, fill=255)

        # Apply mask to alpha channel
        img.putalpha(mask)
        return img
