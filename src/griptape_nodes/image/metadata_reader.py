"""Bounded random-access reads for container metadata without loading pixels."""

from __future__ import annotations

import os
from typing import BinaryIO, Protocol

MAX_METADATA_READ_BYTES = 64 * 1024 * 1024


class MetadataSource(Protocol):
    """Random-access source used by the TIFF metadata parser."""

    @property
    def size(self) -> int:
        """Source extent in bytes."""
        ...

    def read_at(self, offset: int, size: int) -> bytes:
        """Read a bounded range."""
        ...


class FileMetadataSource:
    """Read selected metadata ranges from a borrowed binary handle."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._size = os.fstat(handle.fileno()).st_size
        self._remaining = MAX_METADATA_READ_BYTES

    @property
    def size(self) -> int:
        """File size without loading its contents."""
        return self._size

    def read_at(self, offset: int, size: int) -> bytes:
        """Read an in-file range within the aggregate metadata budget."""
        if offset < 0 or size < 0 or offset > self._size or size > self._size - offset:
            msg = "Attempted to read image metadata. Failed because a metadata block lies outside the image file."
            raise ValueError(msg)
        if size > self._remaining:
            msg = "Attempted to read image metadata. Failed because the image metadata exceeds the size limit."
            raise ValueError(msg)
        self._remaining -= size
        self._handle.seek(offset)
        result = self._handle.read(size)
        if len(result) != size:
            msg = "Attempted to read image metadata. Failed because the image file changed or is incomplete."
            raise ValueError(msg)
        return result


class BytesMetadataSource:
    """Expose encoded bytes through the TIFF parser's random-access interface."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def size(self) -> int:
        """Encoded byte count."""
        return len(self._data)

    def read_at(self, offset: int, size: int) -> bytes:
        """Read bytes already validated by the TIFF parser."""
        return self._data[offset : offset + size]
