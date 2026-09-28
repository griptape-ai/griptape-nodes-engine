"""Turn untagged artifact-shaped dicts back into artifacts.

Tagged values already arrive decoded through the plain-data wire format (see
``griptape_nodes.serialization.values``). This module covers what that format
doesn't: dicts the editor sends straight from its own artifact-shaped JSON, and
values in older saved workflows saved before that format existed. Only a plain
``dict`` (not an ``UndecodedValue``, which is a dict subclass callers rely on
staying a dict) with a ``"type"`` key is treated this way.
"""

from __future__ import annotations

import logging
from typing import Any

from griptape.artifacts import BaseArtifact

logger = logging.getLogger("griptape_nodes")


def hydrate_parameter_values(values: dict[str, Any]) -> dict[str, Any]:
    """Reconstitute untagged artifact dicts in a parameter-value dict."""
    return {name: hydrate_value(value) for name, value in values.items()}


def hydrate_value(value: Any) -> Any:
    """Reconstitute a single untagged artifact dict. Lists are walked element-wise."""
    if type(value) is dict and "type" in value:
        try:
            return BaseArtifact.from_dict(value)
        except Exception:
            logger.debug("Could not hydrate value as artifact; passing through.", exc_info=True)
            return value
    if isinstance(value, list):
        return [hydrate_value(item) for item in value]
    return value
