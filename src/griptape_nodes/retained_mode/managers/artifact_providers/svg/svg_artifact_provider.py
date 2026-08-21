"""SVG artifact provider."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )

logger = logging.getLogger("griptape_nodes")


class SVGArtifactMetadata(BaseArtifactMetadata):
    """Metadata extracted from an SVG source file.

    ``width``/``height`` are the raw attribute text from the root ``<svg>`` element (e.g. ``"100px"``
    or ``"100%"``), not parsed numeric values -- SVG dimensions may carry units or be percentages, and
    unit conversion is out of scope here.
    """

    width: str | None
    height: str | None
    view_box: str | None
    file_size: int


class SVGArtifactProvider(BaseArtifactProvider):
    """Provider for SVG artifacts.

    PIL cannot open SVG files (they're XML, not raster data), so this provider has its own metadata
    extraction and preview generation path instead of delegating to PIL like ImageArtifactProvider does.
    """

    # Generous enough to clear an XML declaration/DOCTYPE prologue while still being a cheap sniff.
    _SNIFF_HEAD_BYTES: ClassVar[int] = 2048

    @classmethod
    def get_friendly_name(cls) -> str:
        return "SVG"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"svg"}

    @classmethod
    def supports_file_extension(cls, file_extension: str) -> bool:
        """Return True if the given file extension (with or without leading dot) is a supported SVG format."""
        return file_extension.lstrip(".").lower() in cls.get_supported_formats()

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"png", "webp"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        from griptape_nodes.retained_mode.managers.artifact_providers.svg.preview_generators import (
            SVGThumbnailGenerator,
        )

        return SVGThumbnailGenerator.get_friendly_name()

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "png"

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes."""
        from griptape_nodes.retained_mode.managers.artifact_providers.svg.preview_generators import (
            SVGThumbnailGenerator,
        )

        return [SVGThumbnailGenerator]

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:
        """Sniff SVG content.

        Unlike raster formats, SVG has no magic bytes -- it's XML text, optionally preceded by a
        BOM/XML declaration or DOCTYPE. Decoding a generous head window and checking for the `<svg`
        tag (the same idea already used ad hoc in `griptape_nodes/utils/image_preview.py`) is single
        source of truth for that sniff here.
        """
        head = data[: cls._SNIFF_HEAD_BYTES].decode("utf-8", errors="ignore").lower()
        if "<svg" in head:
            return "svg"
        return None

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> SVGArtifactMetadata | None:
        """Extract SVG metadata (width/height/viewBox) from the root `<svg>` element.

        PIL can't be used here (see ImageArtifactProvider.get_artifact_metadata for the raster
        equivalent) -- SVG is XML, so this reads the root element's attributes directly.
        """
        path = Path(source_path)
        try:
            # defusedxml guards against XXE/entity-expansion attacks -- SVGs are user-uploaded,
            # untrusted XML, so plain xml.etree is not safe to parse them with.
            root = DefusedET.parse(path).getroot()
            file_size = path.stat().st_size
        except (OSError, ET.ParseError, DefusedXmlException):
            return None

        if root is None:
            return None

        return SVGArtifactMetadata(
            width=root.get("width"),
            height=root.get("height"),
            view_box=root.get("viewBox"),
            file_size=file_size,
        )
