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
    _UTF8_BOM: ClassVar[bytes] = b"\xef\xbb\xbf"

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
        return {"png", "webp", "jpg"}

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

        Unlike raster formats, SVG has no magic bytes -- it's XML text. To stay safe on arbitrary
        payloads (this runs on every byte write), only claim SVG when `<svg` is the first real
        element after optional BOM/XML prologue constructs (whitespace, processing instructions,
        comments, and DOCTYPE).
        """
        print('data: ', data)
        head = data[: cls._SNIFF_HEAD_BYTES]
        print('head: ', head)
        if head.startswith(cls._UTF8_BOM):
            head = head[len(cls._UTF8_BOM) :]

        while True:
            head = head.lstrip()
            lower_head = head.lower()

            if lower_head.startswith(b"<?"):
                end = head.find(b"?>")
                if end < 0:
                    return None
                head = head[end + 2 :]
                continue

            if head.startswith(b"<!--"):
                end = head.find(b"-->")
                if end < 0:
                    return None
                head = head[end + 3 :]
                continue

            if lower_head.startswith(b"<!doctype"):
                i = len(b"<!doctype")
                bracket_depth = 0
                quote: bytes | None = None

                while i < len(head):
                    char = head[i : i + 1]

                    if quote is not None:
                        if char == quote:
                            quote = None
                    else:
                        if char in (b'"', b"'"):
                            quote = char
                        elif char == b"[":
                            bracket_depth += 1
                        elif char == b"]":
                            bracket_depth = max(0, bracket_depth - 1)
                        elif char == b">" and bracket_depth == 0:
                            break

                    i += 1

                end = i if i < len(head) else -1
                if end < 0:
                    return None
                head = head[end + 1 :]
                continue

            return "svg" if lower_head.startswith(b"<svg") and (len(head) == 4 or head[4:5] in b" \t\r\n/>") else None

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
