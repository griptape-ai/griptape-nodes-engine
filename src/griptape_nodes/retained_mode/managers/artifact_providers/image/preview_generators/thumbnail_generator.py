"""Standard image thumbnail generator backed by OpenImageIO."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import PositiveInt  # noqa: TC002 - Runtime validation, not type-only

from griptape_nodes.files.path_utils import is_url
from griptape_nodes.image import (
    DEFAULT_THUMBNAIL_SIZE,
    SUPPORTED_IMAGE_EXTENSIONS,
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
    """Parameters for standard image thumbnail generation."""

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
    """OpenImageIO-backed thumbnail generator with dimension constraints.

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
        super().__init__(
            source_file_location,
            preview_format,
            destination_preview_directory,
            destination_preview_file_name,
            params,
            engine=engine,
        )
        self.params = ThumbnailParameters.model_validate(params)

    async def attempt_generate_preview(self) -> str:
        """Execute preview generation off the event loop, streaming local files through ImageCache."""
        source = await self._resolve_source()
        output_bytes = await to_thread(self._render_thumbnail, source)

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

    async def _resolve_source(self) -> bytes | Path:
        if not is_url(self.source_file_location) and not self.source_file_location.startswith("data:"):
            source_path = Path(self.source_file_location)
            if not await anyio.Path(source_path).is_file():
                msg = f"Failed to read source image: {self.source_file_location}"
                raise FileNotFoundError(msg)
            return source_path

        read_request = ReadFileRequest(
            file_path=self.source_file_location,
            workspace_only=False,
            should_transform_image_content_to_thumbnail=False,
        )
        read_result = await self.engine.ahandle_request(read_request)
        if not isinstance(read_result, ReadFileResultSuccess):
            msg = f"Failed to read source image: {read_result.result_details}"
            raise FileNotFoundError(msg)
        if isinstance(read_result.content, str):
            msg = "Source file is text, not binary image data"
            raise TypeError(msg)
        return read_result.content

    def _render_thumbnail(self, source: bytes | Path) -> bytes:
        """Decode, orient, shrink, and re-encode the image off the event loop."""
        preview_format = ImageFormat.from_name(self.preview_format)
        if preview_format is None:
            msg = f"Attempted to generate a thumbnail. Failed because {self.preview_format} is not a supported image format."
            raise TypeError(msg)
        with GriptapeImage.open(source) as image:
            thumbnail = (
                GriptapeImagePipeline(image)
                .exif_transpose()
                .thumbnail(self.params.max_width, self.params.max_height)
                .end()
            )
        return thumbnail.encode(preview_format, metadata=ImageMetadata(icc_profile=thumbnail.metadata.icc_profile))

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name."""
        return "Standard Thumbnail Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process."""
        return set(SUPPORTED_IMAGE_EXTENSIONS)

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces."""
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return ThumbnailParameters
