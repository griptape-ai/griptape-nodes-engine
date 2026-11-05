"""Checks a node declares on its inputs and the engine runs at the node's turn, before it runs.

Import them from here:

```python
from griptape_nodes.exe_types.validators import IfParameter, MaxCount, PreRunValidator
from griptape_nodes.exe_types.validators import AllowedFormats, MaxFileSize  # limits on media files
```

- `base`: `PreRunValidator`, a limit on any value, checked by `check(value, ctx)`; `IfParameter`,
  for limits that depend on another parameter's value; `Condition`, what `when` and `unless` take to
  narrow any rule; and `PreRunRule`, the type of anything `pre_run_validators=` takes.
- `context`: `CheckContext`, what `check` and a condition are handed: the input's subject and limits
  page, and its media file (`ctx.media`).
- `common`: `MaxCount` for a list's length.
- `media_validator`: `MediaValidator`, the family for image, video and audio files.
- `media`: the built-in media limits, such as `MaxFileSize` or `MaxDuration`.

`limit_error` builds the error a limit returns, with its link to the provider's limits page, for a
custom validator or a rule checked in `validate_before_node_run`. To run a node's checks as the
engine does, in a test, import `validate_node` from `griptape_nodes.exe_types.validators.runner`.
"""

from __future__ import annotations

# Validators for any kind of value.
from griptape_nodes.exe_types.validators._checks import limit_error
from griptape_nodes.exe_types.validators.base import (
    LIMITS_LINK_LABEL,
    Condition,
    IfParameter,
    PreRunRule,
    PreRunValidator,
)
from griptape_nodes.exe_types.validators.common import MaxCount
from griptape_nodes.exe_types.validators.context import CheckContext

# Validators for image, video and audio files.
from griptape_nodes.exe_types.validators.media import (
    AllowedFormats,
    AspectRatioRange,
    AudioCodecs,
    ExactAspectRatios,
    FrameCount,
    FrameRate,
    MaxDuration,
    MaxFileSize,
    MinDuration,
    PixelArea,
    SideRange,
    VideoCodecs,
)
from griptape_nodes.exe_types.validators.media_validator import MediaValidator

__all__ = [
    "LIMITS_LINK_LABEL",
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "CheckContext",
    "Condition",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "IfParameter",
    "MaxCount",
    "MaxDuration",
    "MaxFileSize",
    "MediaValidator",
    "MinDuration",
    "PixelArea",
    "PreRunRule",
    "PreRunValidator",
    "SideRange",
    "VideoCodecs",
    "limit_error",
]
