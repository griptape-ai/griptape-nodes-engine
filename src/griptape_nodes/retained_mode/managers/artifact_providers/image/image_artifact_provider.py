"""Image artifact provider."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from PIL import Image, ImageOps

from griptape_nodes.drivers.image_metadata.image_metadata_driver_registry import (
    ImageMetadataDriverRegistry,
)
from griptape_nodes.files.media_variables import MediaInfo
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import collect_workflow_metadata
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import pillow

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_preview_generator import (
        BaseArtifactPreviewGenerator,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ImageArtifactMetadata(BaseArtifactMetadata):
    """Metadata extracted from the header of an image source file."""

    width: int
    height: int
    format: str
    channels: int
    color_space: str
    file_size: int

    def to_media_info(self) -> MediaInfo:
        return MediaInfo(
            width=self.width,
            height=self.height,
            channels=self.channels,
            color_space=self.color_space,
        )


class ImageArtifactProvider(BaseArtifactProvider):
    """Provider for image artifacts using Pillow for standard thumbnails and source metadata."""

    def __init__(self, registry: ProviderRegistry, engine: Engine | None = None) -> None:
        """Initialize the image artifact provider.

        Args:
            registry: The ProviderRegistry that manages this provider
            engine: The engine this provider belongs to
        """
        super().__init__(registry, engine)
        logger.info("Loaded Image artifact provider: Pillow")

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Image"

    # Minimum bytes needed for the magic-byte sniffer below (the ISO BMFF
    # brand sits at offset 8-12, which is the longest header we inspect).
    _SNIFF_MIN_HEADER_BYTES: ClassVar[int] = 12

    # Maps PIL image mode to (channels, color_space) for metadata reporting
    _PIL_MODE_INFO: ClassVar[dict[str, tuple[int, str]]] = {
        "L": (1, "Grayscale"),
        "P": (1, "Palette"),
        "RGB": (3, "RGB"),
        "RGBA": (4, "RGBA"),
        "CMYK": (4, "CMYK"),
        "YCbCr": (3, "YCbCr"),
        "LAB": (3, "LAB"),
        "HSV": (3, "HSV"),
        "I": (1, "Grayscale"),
        "F": (1, "Grayscale"),
        "LA": (2, "Grayscale+Alpha"),
        "RGBa": (4, "RGBA"),  # spellchecker:disable-line
        "RGBX": (4, "RGB"),
    }

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return pillow.get_supported_source_formats()

    @classmethod
    def supports_file_extension(cls, file_extension: str) -> bool:
        """Return True if the given file extension (with or without leading dot) is a supported image format."""
        return file_extension.lstrip(".").lower() in cls.get_supported_formats()

    @classmethod
    def get_mode_info(cls, mode: str) -> tuple[int, str]:
        """Return (channels, color_space) for a PIL image mode, with a sensible fallback."""
        return cls._PIL_MODE_INFO.get(mode, (3, mode))

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> ImageArtifactMetadata | None:
        """Extract original image metadata via PIL's lazy header read (no full decode)."""
        try:
            path = Path(source_path)
            with Image.open(path) as raw_img:
                img = ImageOps.exif_transpose(raw_img)
                width, height = img.size
                channels, color_space = cls.get_mode_info(img.mode)
                return ImageArtifactMetadata(
                    width=width,
                    height=height,
                    format=(img.format or path.suffix.lstrip(".")).upper(),
                    channels=channels,
                    color_space=color_space,
                    file_size=path.stat().st_size,
                )
        except Exception:
            return None

    @classmethod
    def get_preview_formats(cls) -> set[str]:
        return {"webp", "jpg", "png"}

    @classmethod
    def get_default_preview_generator(cls) -> str:
        return "Standard Thumbnail Generation"

    @classmethod
    def get_default_preview_format(cls) -> str:
        return "webp"

    @classmethod
    def get_default_preview_generators(cls) -> list[type[BaseArtifactPreviewGenerator]]:
        """Get default preview generator classes."""
        # Project templates import this provider, and the generators import events that depend on those templates.
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILRoundedPreviewGenerator,
            PILThumbnailGenerator,
        )

        return [PILThumbnailGenerator, PILRoundedPreviewGenerator]

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:  # noqa: PLR0911
        """Magic-byte sniff for common image formats.

        Pure prefix checks; no PIL parsing or decompression-bomb scans, so this
        is safe to run on every byte write regardless of payload type.
        """
        if len(data) < cls._SNIFF_MIN_HEADER_BYTES:
            return None
        head = data[: cls._SNIFF_MIN_HEADER_BYTES]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if head[:3] == b"\xff\xd8\xff":
            return "jpg"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if head[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":  # noqa: PLR2004
            return "webp"
        if head[:2] == b"BM":
            return "bmp"
        if head[:4] in (b"II*\x00", b"MM\x00*"):
            return "tiff"
        return None

    @classmethod
    def get_metadata_formats(cls) -> set[str]:
        """File extensions that support automatic metadata injection.

        Returns:
            Set of lowercase file extensions WITHOUT leading dots
        """
        return {"png"}

    def collect_metadata_for_write(self, file_name: str, workflow: WorkflowRuntime | None) -> dict[str, str]:
        ext = Path(file_name).suffix.lstrip(".").lower()
        if ext not in self.get_metadata_formats():
            return {}

        try:
            return collect_workflow_metadata(self.engine, workflow)
        except Exception as e:
            logger.warning("Attempted to collect workflow metadata for %s. Failed because: %s", file_name, e)
            return {}

    def prepare_content_for_write(self, data: bytes, file_name: str, metadata: dict[str, str]) -> bytes:  # noqa: PLR0911
        if not metadata:
            return data

        if not data:
            logger.warning("Cannot inject metadata: empty data")
            return data

        with BytesIO(data) as source:
            try:
                pil_image = Image.open(source)
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                logger.warning("Attempted to open image data for %s. Failed because: %s", file_name, e)
                return data
            with pil_image:
                image_format = pil_image.format

        if image_format is None:
            logger.warning("Could not detect image format from data")
            return data

        driver = ImageMetadataDriverRegistry.get_driver_for_format(image_format)
        if driver is None:
            logger.warning("No metadata driver found for format: %s", image_format)
            return data

        # Registered custom drivers can raise their own exceptions. Metadata is best-effort:
        # keep the original content on any driver failure, without hiding process-control errors.
        try:
            encoded = driver.inject_metadata_into_bytes(data, metadata)
        except Exception as e:
            logger.warning("Attempted to inject metadata into %s. Failed because: %s", file_name, e)
            return data
        return encoded
