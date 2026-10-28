"""Checks a node declares on its inputs and the engine runs at the node's turn, before it runs.

Import them from here:

```python
from griptape_nodes.exe_types.validators import AllowedFormats, ByParameter, MaxFileSize
```

- `base`: what a validator is (`PreRunValidator`, `NodeValidator`) and what the engine reads for it
  (`MediaNeed`, `MediaInfo`).
- `media`: limits on an image, video or audio file, such as `MaxFileSize` or `Duration`.
- `common`: `MaxCount` for a list's length, and `ByParameter` for limits that depend on another
  parameter's value.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.validators.base import MediaInfo, MediaNeed, NodeValidator, PreRunValidator

if TYPE_CHECKING:
    from griptape_nodes.exe_types.validators.common import ByParameter, MaxCount
    from griptape_nodes.exe_types.validators.media import (
        AllowedFormats,
        AspectRatioRange,
        AudioCodecs,
        Duration,
        ExactAspectRatios,
        FrameCount,
        FrameRate,
        MaxFileSize,
        PixelArea,
        SideRange,
        VideoCodecs,
    )

__all__ = [
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "ByParameter",
    "Duration",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "MaxCount",
    "MaxFileSize",
    "MediaInfo",
    "MediaNeed",
    "NodeValidator",
    "PixelArea",
    "PreRunValidator",
    "SideRange",
    "VideoCodecs",
]

# The module each `media` and `common` validator is loaded from on first use. They can't be imported
# above: `Parameter` imports `validators.base`, which runs this file first, and `media` and `common`
# import `input_checks`, whose imports lead back to `Parameter` before it is defined.
_LAZY_MODULES: dict[str, str] = {
    "AllowedFormats": "media",
    "AspectRatioRange": "media",
    "AudioCodecs": "media",
    "Duration": "media",
    "ExactAspectRatios": "media",
    "FrameCount": "media",
    "FrameRate": "media",
    "MaxFileSize": "media",
    "PixelArea": "media",
    "SideRange": "media",
    "VideoCodecs": "media",
    "ByParameter": "common",
    "MaxCount": "common",
}


def __getattr__(name: str) -> Any:
    """Load a `media` or `common` validator on first use (avoid circular import)."""
    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
