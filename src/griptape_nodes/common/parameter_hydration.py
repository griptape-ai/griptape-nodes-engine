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
from typing import TYPE_CHECKING, Any

from griptape.artifacts import BaseArtifact

if TYPE_CHECKING:
    from collections.abc import Mapping

    from griptape_nodes.exe_types.node_types import BaseNode

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


def dehydrate_parameter_values(values: Mapping[str, Any], *, node: BaseNode, are_outputs: bool) -> dict[str, Any]:
    """Parameter values with anything the cache is holding replaced by its key.

    The outbound half of this module. Called where values are about to leave the process -- a worker
    dispatch or a worker result -- and nowhere else, so a node's own dicts keep the real objects and a
    graph that never crosses a boundary never caches anything.

    An output whose author declared it unpersistable goes in the cache unless the value is already plain
    data: a key for an API token would be unresolvable on the far side, while anything richer is held
    rather than unstructured, because cattrs turns any attrs class into a dict of its fields and the
    inbound mirror cannot put it back. Every other value is passed through exactly as it was before this
    existed, including one the transport can only manage by stringifying it.
    """
    dehydrated: dict[str, Any] = {}
    # A copy, because node bodies write their outputs from worker threads and a dict that changes size
    # mid-iteration raises. `_keys_referenced_by` snapshots for the same reason.
    for name, value in dict(values).items():
        parameter = node.get_parameter_by_name(name)
        if are_outputs and parameter is not None and parameter.is_process_local:
            dehydrated[name] = node.park_for_egress(parameter, value, travels_as_data=_is_json_safe(value))
            continue
        dehydrated[name] = value
    return dehydrated


def _is_json_safe(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool, type(None))):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        # json.dumps coerces int/float/bool/None keys rather than refusing them, so a dict keyed by frame
        # number travels fine and must not be reported as unsendable.
        return all(isinstance(k, (str, int, float, bool)) or k is None for k in value) and all(
            _is_json_safe(v) for v in value.values()
        )
    return False
