"""The exception a node raises to send its failure to the editor in parts.

Node libraries import ``NodeError`` and ``NodeErrorLink`` from ``griptape_nodes.exe_types.core_types``.
This module imports only the standard library: ``event_converter`` imports it to serialize a
``NodeError`` across the worker boundary, and ``base_events`` imports ``event_converter``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NodeErrorLink:
    label: str
    """Link text, e.g. "Supported image formats"."""

    url: str
    """An http or https page, or a place in the editor starting with "#", such as
    "#settings-secrets?filter=MY_KEY". URL-encode any values in a "#" link's query.

    The engine accepts any "#" link and passes it through. The editor decides which places it
    opens, and only follows routes that navigate, never ones that change anything."""


class NodeError(Exception):
    """A node failure with parts the editor shows on their own lines.

    Raise it from a node instead of building those parts into the message:

        raise NodeError(
            "Processing failed: proxy client error",
            fields={"generation_id": generation_id},
            response=response_json,
        )

    Args:
        message: What went wrong, for an artist. Leave out the node's name; the editor shows it.
        fields: Labelled values a user may need to quote to support, such as a request ID.
            Values are shown as text.
        response: The provider's response body. Never include headers. Dropped if it is not
            JSON-serializable or is larger than 16 KB.
        links: Up to three http(s) pages that explain the failure, or "#" links that open the
            place in the editor where the user can fix it.
    """

    def __init__(
        self,
        message: str,
        *,
        fields: dict[str, str | int | float | bool] | None = None,
        response: dict[str, Any] | None = None,
        links: list[NodeErrorLink] | None = None,
    ) -> None:
        super().__init__(message)
        self.fields = fields or {}
        self.response = response
        self.links = links or []
