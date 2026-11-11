"""Thumbnail generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import PositiveInt  # noqa: TC002 - Runtime validation, not type-only

from griptape_nodes.image import (
    DEFAULT_THUMBNAIL_SIZE,
    GriptapeImage,
    GriptapeImagePipeline,
    ImageFormat,
    ImageMetadata,
)
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


class ThumbnailParameters(BaseGeneratorParameters):
    """Parameters for thumbnail generation."""

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


class ThumbnailGenerator(BaseArtifactPreviewGenerator):
    """Thumbnail generator with dimension constraints.

    Resizes images to fit within max_width x max_height while preserving aspect ratio.
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
        """Initialize the generator.

        Args:
            source_file_location: Path to the source image file
            preview_format: Target format (webp, jpg, png)
            destination_preview_directory: Directory where the preview should be saved
            destination_preview_file_name: Filename for the preview
            params: Generator parameters (max_width, max_height)
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
        self.params = ThumbnailParameters.model_validate(params)

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name."""
        return "Standard Thumbnail Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process."""
        return {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces."""
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return ThumbnailParameters

    async def attempt_generate_preview(self) -> str:
        """Execute preview generation.

        Raises:
            FileNotFoundError: If source image not found
            TypeError: If image cannot be loaded or format unsupported
            OSError: If preview generation fails (image decode or encode errors)
        """
        # Read the source image file
        read_request = ReadFileRequest(
            file_path=self.source_file_location,
            workspace_only=False,
            should_transform_image_content_to_thumbnail=False,
        )
        read_result = await self.engine.ahandle_request(read_request)

        if not isinstance(read_result, ReadFileResultSuccess):
            msg = f"Failed to read source image: {read_result.result_details}"
            raise FileNotFoundError(msg)

        # Type guard: read_result is now ReadFileResultSuccess
        image_data = read_result.content
        if isinstance(image_data, str):
            msg = "Source file is text, not binary image data"
            raise TypeError(msg)

        output_bytes = await to_thread(self._render_thumbnail, image_data)

        # Construct full path for writing
        destination_path = str(Path(self.destination_preview_directory) / self.destination_preview_file_name)

        # Write the preview file
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

    def _render_thumbnail(self, image_data: bytes) -> bytes:
        """Decode, shrink and re-encode the image. CPU-bound, so callers run it off the event loop."""
        preview_format = ImageFormat.from_name(self.preview_format)
        if preview_format is None:
            msg = (
                f"Attempted to generate a thumbnail. Failed because {self.preview_format} is not a known image format."
            )
            raise TypeError(msg)
        with GriptapeImage.open(image_data) as source:
            # Apply EXIF orientation so rotated images (e.g. phone photos) display correctly, then
            # fit within the max dimensions, preserving aspect ratio
            thumbnail = (
                GriptapeImagePipeline(source)
                .exif_transpose()
                .thumbnail(self.params.max_width, self.params.max_height)
                .end()
            )
        # The color profile keeps colors right; embedded workflows and camera EXIF stay out of previews
        return thumbnail.encode(preview_format, metadata=ImageMetadata(icc_profile=thumbnail.metadata.icc_profile))
