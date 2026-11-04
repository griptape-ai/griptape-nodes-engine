"""Request and result payloads this library owns.

Both `advanced_library.py` (which serves the request) and `nodes.py` (which sends it)
import this module, so the payload classes they use are the same objects. The library
directory is on `sys.path` by the time either file loads, which makes the plain
`import colorspace_events` work.

Give this file a distinctive name. Every library directory lands on the same `sys.path`,
so a generic name like `events.py` risks resolving to a different library's file.
"""

from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

# Colorspaces this library knows how to convert between.
RGB = "rgb"
HSV = "hsv"


@dataclass
@PayloadRegistry.register
class ConvertColorspaceRequest(RequestPayload):
    """Convert a color between this library's supported colorspaces.

    Args:
        color: Three channel values in 0.0-1.0.
        source: Colorspace `color` is currently in ("rgb" or "hsv").
        target: Colorspace to convert to ("rgb" or "hsv").

    Results: ConvertColorspaceResultSuccess | ConvertColorspaceResultFailure
    """

    color: tuple[float, float, float]
    source: str
    target: str


@dataclass
@PayloadRegistry.register
class ConvertColorspaceResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Color converted successfully.

    Args:
        color: The converted three channel values in 0.0-1.0.
    """

    color: tuple[float, float, float]


@dataclass
@PayloadRegistry.register
class ConvertColorspaceResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Conversion could not be performed."""
