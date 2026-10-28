"""Checks a node declares on its inputs and the engine runs at the node's turn, before it runs.

Import them from here:

```python
from griptape_nodes.exe_types.validators import AllowedFormats, DependsOn, MaxFileSize
```

- `base`: what a validator is (`PreRunValidator`) and what the engine reads for it (`MediaNeed`,
  `MediaInfo`, and `EMPTY_MEDIA` when it read nothing), and `reads` to mark what a `Condition`
  for `PreRunValidator.when` or `unless` reads.
- `media`: limits on an image, video or audio file, such as `MaxFileSize` or `Duration`.
- `common`: `MaxCount` for a list's length, and `DependsOn` for limits that depend on another
  parameter's value.
"""

from __future__ import annotations

from griptape_nodes.exe_types.validators.base import (
    EMPTY_MEDIA,
    LIMITS_LINK_LABEL,
    Condition,
    MediaInfo,
    MediaNeed,
    PreRunValidator,
    reads,
)
from griptape_nodes.exe_types.validators.common import DependsOn, MaxCount
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
    "EMPTY_MEDIA",
    "LIMITS_LINK_LABEL",
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "Condition",
    "DependsOn",
    "Duration",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "MaxCount",
    "MaxFileSize",
    "MediaInfo",
    "MediaNeed",
    "PixelArea",
    "PreRunValidator",
    "SideRange",
    "VideoCodecs",
    "reads",
]
