"""Shared request orchestration for standard thumbnail backends."""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.events.os_events import (
    ReadFileRequest,
    ReadFileResultSuccess,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import GetPathForMacroRequest, GetPathForMacroResultSuccess
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
    BaseArtifactPreviewGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_parameters import (
    ThumbnailParameters,
)
from griptape_nodes.utils.async_utils import to_thread

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class BaseThumbnailGenerator(BaseArtifactPreviewGenerator):
    """Dispatch byte reads/writes around backend-specific rendering."""

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

    async def attempt_generate_preview(self) -> str:
        """Read and publish bytes through engine requests, rendering off the event loop."""
        read = await self.engine.ahandle_request(
            ReadFileRequest(
                file_path=self.source_file_location,
                workspace_only=False,
                should_transform_image_content_to_thumbnail=False,
            )
        )
        if not isinstance(read, ReadFileResultSuccess):
            raise OSError(str(read.result_details))
        if not isinstance(read.content, bytes):
            msg = f"Attempted to load image '{self.source_file_location}'. Failed due to the source containing text instead of image bytes."
            raise OSError(msg)
        temp_result = await self.engine.ahandle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{temp}"), variables={})
        )
        if not isinstance(temp_result, GetPathForMacroResultSuccess):
            msg = f"Attempted to prepare temporary encoding for '{self.source_file_location}'. Failed due to {temp_result.result_details}."
            raise OSError(msg)
        preserve_text = self.engine.config_manager.get_config_value(
            "preserve_source_metadata_in_thumbnails", default=False, cast_type=bool
        )
        max_source_pixels = self.engine.config_manager.get_config_value("max_thumbnail_source_pixels", cast_type=int)
        content = await to_thread(
            self._encode_thumbnail,
            read.content,
            temp_result.absolute_path,
            preserve_text=preserve_text,
            max_source_pixels=max_source_pixels,
        )
        destination = str(Path(self.destination_preview_directory) / self.destination_preview_file_name)
        result = await self.engine.ahandle_request(WriteFileRequest(file_path=destination, content=content))
        if not isinstance(result, WriteFileResultSuccess):
            msg = f"Attempted to write preview '{destination}' for '{self.source_file_location}'. Failed due to {result.result_details}."
            raise OSError(msg)
        return Path(result.final_file_path).name

    @abstractmethod
    def _encode_thumbnail(
        self, source_bytes: bytes, temp_directory: Path, *, preserve_text: bool, max_source_pixels: int
    ) -> bytes:
        """Return encoded thumbnail bytes without publishing them."""
