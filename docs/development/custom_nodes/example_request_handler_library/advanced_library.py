"""Advanced library that serves this library's `ConvertColorspaceRequest`.

The engine calls `get_request_handlers()` after this library's nodes are loaded,
registers each returned pair with the event bus, and deregisters them automatically
when the library is unloaded. From then on any caller in the orchestrator process can
dispatch `ConvertColorspaceRequest` and reach `_handle_convert_colorspace`.
"""

from __future__ import annotations

import colorsys
from typing import TYPE_CHECKING

from colorspace_events import (
    HSV,
    RGB,
    ConvertColorspaceRequest,
    ConvertColorspaceResultFailure,
    ConvertColorspaceResultSuccess,
)

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

SUPPORTED = (RGB, HSV)


class ColorspaceServiceLibrary(AdvancedNodeLibrary):
    def get_request_handlers(self) -> list[tuple[type[RequestPayload], Callable]]:
        """Declare the request types this library serves.

        The return type uses a bare `Callable` on purpose. The base class declares
        `Callable[[RequestPayload], ResultPayload]`, and a handler annotated with a
        concrete request type is not assignable to that because parameter types are
        contravariant. Keeping the handler's own annotation precise is worth more than
        matching the base signature exactly.
        """
        return [(ConvertColorspaceRequest, self._handle_convert_colorspace)]

    def _handle_convert_colorspace(self, request: ConvertColorspaceRequest) -> ResultPayload:
        """Convert between RGB and HSV.

        Validation first, success path last: every failure returns immediately with a
        message an artist can act on.
        """
        if request.source not in SUPPORTED:
            return ConvertColorspaceResultFailure(
                result_details=(
                    f"Attempted to convert a color from '{request.source}'. Failed because that "
                    f"colorspace is not supported. Supported colorspaces: {', '.join(SUPPORTED)}."
                )
            )
        if request.target not in SUPPORTED:
            return ConvertColorspaceResultFailure(
                result_details=(
                    f"Attempted to convert a color to '{request.target}'. Failed because that "
                    f"colorspace is not supported. Supported colorspaces: {', '.join(SUPPORTED)}."
                )
            )
        if len(request.color) != len(("h", "s", "v")):
            return ConvertColorspaceResultFailure(
                result_details=(
                    f"Attempted to convert a color with {len(request.color)} channels. Failed "
                    f"because a color must have exactly 3 channels."
                )
            )

        first, second, third = request.color
        if request.source == request.target:
            converted = (first, second, third)
        elif request.target == HSV:
            converted = colorsys.rgb_to_hsv(first, second, third)
        else:
            converted = colorsys.hsv_to_rgb(first, second, third)

        return ConvertColorspaceResultSuccess(
            color=converted,
            result_details=f"Converted a color from {request.source} to {request.target}.",
        )
