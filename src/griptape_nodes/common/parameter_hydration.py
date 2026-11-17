"""Turn untagged artifact dicts, as the editor and older saved workflows send them, into artifacts.

Tagged values arrive decoded, and ``UndecodedValue`` (a dict subclass) is left alone. Tracked to move
to set time in https://github.com/griptape-ai/griptape-nodes-engine/issues/5695.
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
