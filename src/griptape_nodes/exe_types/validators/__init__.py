"""Checks a node declares on its inputs and the engine runs at the node's turn, before it runs.

Import them from here:

```python
from griptape_nodes.exe_types.validators import IfParameter, MaxCount, PreRunValidator
from griptape_nodes.exe_types.validators import AllowedFormats, MaxFileSize  # limits on media files
```

- `base`: `PreRunValidator`, a limit on any value.
- `common`: `MaxCount` for a list's length, and `IfParameter` for limits that depend on another
  parameter's value.
- `media_validator`: `MediaValidator`, the family for image, video and audio files, judged on
  readings the engine takes once (`MediaNeed` names one, `MediaInfo` holds them, both from
  `griptape_nodes.utils.media_probe`), and `reads` to mark what a `Condition` for
  `MediaValidator.when` or `unless` reads.
- `media`: the built-in media limits, such as `MaxFileSize` or `MaxDuration`.

Two helpers for a node's own checks: `limit_error` builds the error a limit returns, with its link
to the provider's limits page, for a rule checked in `validate_before_node_run`; `validate_node` runs
a node's pre-run checks as the engine does at its turn, for testing them.
"""

from __future__ import annotations

# Validators for any kind of value.
from griptape_nodes.exe_types.validators._checks import limit_error
from griptape_nodes.exe_types.validators._runner import validate_node
from griptape_nodes.exe_types.validators.base import LIMITS_LINK_LABEL, PreRunValidator
from griptape_nodes.exe_types.validators.common import IfParameter, MaxCount

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
from griptape_nodes.exe_types.validators.media_validator import Condition, MediaValidator, reads
from griptape_nodes.utils.media_probe import MediaInfo, MediaNeed

__all__ = [
    "LIMITS_LINK_LABEL",
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "Condition",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "IfParameter",
    "MaxCount",
    "MaxDuration",
    "MaxFileSize",
    "MediaInfo",
    "MediaNeed",
    "MediaValidator",
    "MinDuration",
    "PixelArea",
    "PreRunValidator",
    "SideRange",
    "VideoCodecs",
    "limit_error",
    "reads",
    "validate_node",
]
