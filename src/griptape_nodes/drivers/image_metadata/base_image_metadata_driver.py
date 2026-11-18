"""Base class for image metadata injection and extraction drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

from griptape_nodes.image import GriptapeImage, ImageFormat

MetadataImageInput = GriptapeImage | bytes | Path | str | Any


@contextmanager
def opening_metadata_image(image: MetadataImageInput) -> Iterator[GriptapeImage]:
    """Yield a ``GriptapeImage`` for ``image``, closing any temporary wrapper on exit.

    Accepts ``GriptapeImage``, ``bytes``, ``Path``, ``str``, or a legacy duck-typed image object
    exposing ``.format`` and ``.save(buffer, format=...)`` (such as ``PIL.Image.Image`` from
    separately-versioned node libraries) without importing ``PIL``.

    Raises:
        TypeError: If ``image`` is none of the supported input shapes.
    """
    if isinstance(image, GriptapeImage):
        yield image
        return

    if isinstance(image, (bytes, Path, str)):
        with GriptapeImage.open(image) as opened:
            yield opened
        return

    save_fn = getattr(image, "save", None)
    fmt = getattr(image, "format", None)
    if not callable(save_fn) or not isinstance(fmt, str) or not fmt:
        msg = f"Unsupported image input for metadata driver: {type(image).__name__}"
        raise TypeError(msg)

    buffer = BytesIO()
    getexif_fn = getattr(image, "getexif", None)
    exif_obj = getexif_fn() if callable(getexif_fn) else None
    if exif_obj:
        try:
            save_fn(buffer, format=fmt, exif=exif_obj)
        except TypeError:
            buffer = BytesIO()
            save_fn(buffer, format=fmt)
    else:
        save_fn(buffer, format=fmt)

    with GriptapeImage.open(buffer.getvalue()) as opened:
        raw_info = getattr(image, "info", None)
        if opened.format == ImageFormat.PNG and isinstance(raw_info, Mapping):
            info_text = {k: v for k, v in raw_info.items() if isinstance(k, str) and isinstance(v, str)}
            if info_text:
                merged_text = {**info_text, **opened.metadata.text}
                opened._metadata = replace(opened.metadata, text=merged_text)
        yield opened


class BaseImageMetadataDriver(ABC):
    """Base class for bidirectional image metadata drivers.

    Each driver handles a specific metadata protocol (for example PNG text chunks or EXIF).
    Drivers support both injection (writing) and extraction (reading) of metadata.
    """

    @abstractmethod
    def get_supported_formats(self) -> list[str]:
        """Return list of uppercase format strings this driver supports."""
        ...

    @abstractmethod
    def inject_metadata(self, image: MetadataImageInput, metadata: dict[str, str]) -> bytes:
        """Inject metadata into ``image`` and return modified image bytes."""
        ...

    @abstractmethod
    def extract_metadata(self, image: MetadataImageInput) -> dict[str, str]:
        """Extract all available metadata from ``image`` as string key-value pairs."""
        ...
