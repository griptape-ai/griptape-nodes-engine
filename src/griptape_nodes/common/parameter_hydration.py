"""Rehydrate serialized artifacts in parameter-value dicts.

Parameter values cross JSON boundaries between the orchestrator and worker
processes. SerializableMixin instances are unstructured via `to_dict()` on
send, producing dicts like ``{"type": "VideoUrlArtifact", "value": "..."}``.

cattrs dispatches structure hooks by target type. ``parameter_values`` and
``parameter_output_values`` are typed ``dict[str, Any]`` because the set of
valid parameter types is user-extensible (any node library can introduce
new artifact types). With ``Any`` as the target, cattrs has nothing to
dispatch on, so a ``SerializableMixin`` structure hook in
``event_converter`` would never fire for these fields. Registering a
broader ``Any`` hook would fire for every ``Any``-typed field across every
event -- too much collateral damage.

This module is the targeted post-structure pass for exactly those fields.
"""

from __future__ import annotations

import logging
from typing import Any

from griptape.artifacts import BaseArtifact

logger = logging.getLogger(__name__)


def hydrate_parameter_values(values: dict[str, Any]) -> dict[str, Any]:
    """Reconstitute serialized artifacts in a parameter-value dict.

    Walks the dict and replaces any value that looks like a serialized
    SerializableMixin (dict with a ``"type"`` key that resolves to an
    artifact subclass) with the reconstituted object. Lists are walked
    element-wise so parameters like ``list[VideoUrlArtifact]`` work.
    Non-matching values pass through unchanged.
    """
    return {name: hydrate_value(value) for name, value in values.items()}


# TODO: This is hacky and needs to be solved for non-griptape artifacts as well: https://github.com/griptape-ai/griptape-nodes/issues/4475
def hydrate_value(value: Any) -> Any:
    """Reconstitute a single serialized artifact value.

    Replaces a value that looks like a serialized SerializableMixin (dict with
    a ``"type"`` key that resolves to an artifact subclass) with the
    reconstituted object. Lists are walked element-wise. Non-matching values
    pass through unchanged.
    """
    if isinstance(value, dict) and "type" in value:
        try:
            return BaseArtifact.from_dict(value)
        except Exception:
            logger.debug("Could not hydrate value as artifact; passing through.", exc_info=True)
            return value
    if isinstance(value, list):
        return [hydrate_value(item) for item in value]
    return value
