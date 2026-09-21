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

from griptape_nodes.retained_mode.events.event_converter import safe_unstructure

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
    """Parameter values with anything that cannot cross a process boundary replaced by a key.

    The outbound half of this module. Called where values are about to leave the process -- a worker
    dispatch or a worker result -- and nowhere else, so a node's own dicts keep the real objects and a
    graph that never crosses a boundary never parks anything.

    Sendability decides, and the parameter's declaration only authorizes: a value that survives JSON is
    sent as-is even on a `serializable=False` parameter, because a key would be unresolvable on the far
    side. Only a value that cannot survive is held, and only where its author said it is unpersistable.

    Raises:
        TypeError: if an unsendable value sits on a parameter that did not declare `serializable=False`.
            The transport coerces with `str()`, so without this the receiver silently gets a repr.
    """
    dehydrated: dict[str, Any] = {}
    for name, value in values.items():
        parameter = node.get_parameter_by_name(name)
        sendable = _is_sendable(value)
        if parameter is not None and parameter.is_process_local:
            dehydrated[name] = node.park_for_egress(parameter, value, is_output=are_outputs, sendable=sendable)
            continue
        if not sendable:
            msg = (
                f"Attempted to send the value of parameter '{name}' on node '{node.name}' to another "
                f"process. Failed due to: a '{type(value).__name__}' cannot be converted to data. Mark "
                f"that parameter serializable=False so the value is kept here and passed by reference."
            )
            raise TypeError(msg)
        dehydrated[name] = value
    return dehydrated


def _is_sendable(value: Any) -> bool:
    """Whether `value` survives the trip as data.

    The raw check first, because most parameter values are already plain data and the unstructure pass is
    not free. Artifacts only look unsendable until cattrs turns them into dicts, so a failure there is
    retried against the unstructured form rather than believed.
    """
    if _is_json_safe(value):
        return True
    return _is_json_safe(safe_unstructure(value))


def _is_json_safe(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool, type(None))):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_safe(v) for k, v in value.items())
    return False
