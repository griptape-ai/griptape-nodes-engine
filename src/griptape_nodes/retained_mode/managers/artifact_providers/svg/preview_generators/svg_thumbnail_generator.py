"""SVG-to-raster thumbnail generator using resvg-py."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import resvg_py
from defusedxml import ElementTree
from PIL import Image
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

# JPEG has no alpha channel -- flatten onto white instead of leaving resvg's default transparent canvas.
_JPEG_BACKGROUND = "#ffffff"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


class SVGThumbnailParameters(BaseGeneratorParameters):
    """Parameters for SVG thumbnail generation."""

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


class SVGThumbnailGenerator(BaseArtifactPreviewGenerator):
    """Rasterizes SVG sources to a bitmap thumbnail using resvg-py.

    resvg renders directly to PNG bytes, fitting within max_width x max_height while preserving
    aspect ratio (matching PIL's `Image.thumbnail` semantics). Non-PNG preview formats are produced
    by re-encoding that PNG with PIL.
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
            source_file_location: Path to the source SVG file
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
        self.params = SVGThumbnailParameters.model_validate(params)

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name."""
        return "SVG Thumbnail Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process."""
        return {"svg"}

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces."""
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return SVGThumbnailParameters

    async def attempt_generate_preview(self) -> str:
        """Execute preview generation.

        Raises:
            FileNotFoundError: If source SVG not found
            TypeError: If SVG cannot be parsed/rendered
            OSError: If preview generation or write fails
        """
        read_request = ReadFileRequest(
            file_path=self.source_file_location,
            workspace_only=False,
            should_transform_image_content_to_thumbnail=False,
        )
        read_result = await self.engine.ahandle_request(read_request)

        if not isinstance(read_result, ReadFileResultSuccess):
            msg = f"Failed to read source SVG: {read_result.result_details}"
            raise FileNotFoundError(msg)

        svg_data = read_result.content
        svg_text = svg_data.decode("utf-8") if isinstance(svg_data, bytes) else svg_data
        self._validate_embedded_href_safety(svg_text)

        is_jpeg_target = self.preview_format.lower() in ("jpg", "jpeg")
        try:
            png_bytes = resvg_py.svg_to_bytes(
                svg_string=svg_text,
                width=self.params.max_width,
                height=self.params.max_height,
                background=_JPEG_BACKGROUND if is_jpeg_target else None,
            )
        except ValueError as e:
            msg = f"Failed to rasterize SVG: {e}"
            raise TypeError(msg) from e

        if self.preview_format.lower() == "png":
            output_bytes = png_bytes
        else:
            with Image.open(BytesIO(png_bytes)) as img:
                save_img = img.convert("RGB") if is_jpeg_target else img
                output_buffer = BytesIO()
                pil_format = "JPEG" if is_jpeg_target else self.preview_format.upper()
                save_img.save(output_buffer, format=pil_format)
                output_bytes = output_buffer.getvalue()

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

    @staticmethod
    def _validate_embedded_href_safety(svg_text: str) -> None:
        """Reject SVGs that use non-data hrefs in image/use elements."""
        try:
            root = ElementTree.fromstring(svg_text)
        except ElementTree.ParseError as e:
            msg = f"Invalid SVG XML: {e}"
            raise TypeError(msg) from e

        for element in root.iter():
            tag = element.tag
            local_name = tag.rsplit("}", 1)[-1] if "}" in tag else tag
            if local_name not in {"image", "use"}:
                continue

            href = element.attrib.get("href") or element.attrib.get(_XLINK_HREF)
            if href is None:
                continue

            if not href.strip().lower().startswith("data:"):
                msg = "Unsafe SVG: only data: URIs are allowed in image/use href attributes"
                raise TypeError(msg)
