"""The data type of image pixels."""

from __future__ import annotations

from enum import StrEnum

import numpy as np


class PixelType(StrEnum):
    """The data type of one channel of one pixel."""

    UINT8 = "uint8"
    INT8 = "int8"
    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    HALF = "half"
    FLOAT = "float"
    DOUBLE = "double"

    @property
    def bits(self) -> int:
        """Bits per channel."""
        return _PIXEL_TYPE_BITS[self]

    @property
    def is_float(self) -> bool:
        """True for floating-point types, whose nominal range is 0-1 rather than the integer range."""
        return self in (PixelType.HALF, PixelType.FLOAT, PixelType.DOUBLE)

    @classmethod
    def from_numpy(cls, dtype: np.dtype) -> PixelType:
        """Return the pixel type of a NumPy array dtype.

        Raises:
            ValueError: If no pixel type matches ``dtype``
        """
        name = _NUMPY_PIXEL_TYPES.get(np.dtype(dtype).name)
        if name is None:
            msg = f"Attempted to read an image's pixel type. Failed because {dtype} is not a supported pixel type."
            raise ValueError(msg)
        return cls(name)


_PIXEL_TYPE_BITS = {
    PixelType.UINT8: 8,
    PixelType.INT8: 8,
    PixelType.UINT16: 16,
    PixelType.INT16: 16,
    PixelType.UINT32: 32,
    PixelType.INT32: 32,
    PixelType.HALF: 16,
    PixelType.FLOAT: 32,
    PixelType.DOUBLE: 64,
}

_NUMPY_PIXEL_TYPES = {
    "uint8": "uint8",
    "int8": "int8",
    "uint16": "uint16",
    "int16": "int16",
    "uint32": "uint32",
    "int32": "int32",
    "float16": "half",
    "float32": "float",
    "float64": "double",
}
