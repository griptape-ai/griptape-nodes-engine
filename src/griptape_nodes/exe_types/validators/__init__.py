"""Checks a node declares on its inputs and the engine runs at the node's turn, before it runs.

Import them from here:

```python
from griptape_nodes.exe_types.validators import AllowedFormats, IfParameter, MaxFileSize
```

- `base`: what a validator is. `PreRunValidator` is a limit on any value; `MediaValidator` is the
  family for image, video and audio files, judged on readings the engine takes once (`MediaNeed`
  names one, `MediaInfo` holds them, both from `griptape_nodes.utils.media_probe`). `reads` marks
  what a `Condition` for `MediaValidator.when` or `unless` reads.
- `media`: limits on an image, video or audio file, such as `MaxFileSize` or `Duration`.
- `common`: `MaxCount` for a list's length, and `IfParameter` for limits that depend on another
  parameter's value.
"""

from __future__ import annotations

from griptape_nodes.exe_types.validators.base import (
    LIMITS_LINK_LABEL,
    Condition,
    MediaValidator,
    PreRunValidator,
    reads,
)
from griptape_nodes.exe_types.validators.common import IfParameter, MaxCount
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
from griptape_nodes.utils.media_probe import EMPTY_MEDIA, MediaInfo, MediaNeed

__all__ = [
    "EMPTY_MEDIA",
    "LIMITS_LINK_LABEL",
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "Condition",
    "Duration",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "IfParameter",
    "MaxCount",
    "MaxFileSize",
    "MediaInfo",
    "MediaNeed",
    "MediaValidator",
    "PixelArea",
    "PreRunValidator",
    "SideRange",
    "VideoCodecs",
    "reads",
]
