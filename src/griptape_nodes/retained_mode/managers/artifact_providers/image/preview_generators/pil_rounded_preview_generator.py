"""Rounded image preview generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import PositiveInt  # noqa: TC002 - Runtime validation, not type-only

from griptape_nodes.image import DEFAULT_THUMBNAIL_SIZE, GriptapeImagePipeline, ImageFormat, open_image
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
from griptape_nodes.utils.async_utils import to_thread

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class PILRoundedParameters(BaseGeneratorParameters):
    """Parameters for PIL rounded preview generation."""

    max_width: PositiveInt = Field(
        default=DEFAULT_THUMBNAIL_SIZE,
        description="Maximum width in pixels for generated preview (1-8192)",
        editor_schema_type="integer",
        le=8192,
    )

    max_height: PositiveInt = Field(
        default=DEFAULT_THUMBNAIL_SIZE,
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

    Creates thumbnails with rounded corners. Corners are transparent
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

        # Step 3: Process the image off the event loop
        output_bytes = await to_thread(self._render_rounded_preview, image_data)

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

    def _render_rounded_preview(self, image_data: bytes) -> bytes:
        """Decode, shrink, round the corners of and re-encode the image. CPU-bound, so callers run it off the event loop."""
        preview_format = ImageFormat.from_name(self.preview_format)
        if preview_format is None:
            msg = f"Attempted to generate a rounded preview. Failed because {self.preview_format} is not a known image format."
            raise TypeError(msg)
        with open_image(image_data) as source, GriptapeImagePipeline(source) as pipeline:
            # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly, then
            # fit within the max dimensions, preserving aspect ratio
            pipeline.exif_transpose().thumbnail(self.params.max_width, self.params.max_height)
            pipeline.round_corners(self.params.corner_radius_percent / 100.0)
            if preview_format == ImageFormat.JPEG:
                # JPEG doesn't support transparency - composite onto white background
                pipeline.flatten((255, 255, 255))
            with pipeline.end() as preview:
                return preview.encode(preview_format)
