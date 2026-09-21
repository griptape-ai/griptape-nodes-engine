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

from griptape_nodes.exe_types.elements.containers import ParameterContainer
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

    An output whose author declared it unpersistable is held unless the value is already plain data -- a
    key for an API token would only be unresolvable on the far side, while anything richer is held rather
    than unstructured, because cattrs turns any attrs class into a dict of its fields and the inbound
    mirror cannot put it back. Everything else is sent, and refused only if the transport could not encode
    it at all.

    Raises:
        TypeError: if an unsendable value sits on a parameter that did not declare `serializable=False`.
            The transport coerces with `str()`, so without this the receiver silently gets a repr.
    """
    dehydrated: dict[str, Any] = {}
    # A copy, because node bodies write their outputs from worker threads and a dict that changes size
    # mid-iteration raises. `_keys_referenced_by` snapshots for the same reason.
    for name, value in dict(values).items():
        parameter = node.get_parameter_by_name(name)
        if are_outputs and parameter is not None and parameter.is_process_local:
            dehydrated[name] = node.park_for_egress(parameter, value, travels_as_data=_is_json_safe(value))
            continue
        if not _is_sendable(value):
            raise TypeError(
                _unsendable_message(
                    name,
                    value,
                    node_name=node.name,
                    is_output=are_outputs,
                    is_container=isinstance(parameter, ParameterContainer),
                )
            )
        dehydrated[name] = value
    return dehydrated


def _unsendable_message(name: str, value: Any, *, node_name: str, is_output: bool, is_container: bool) -> str:
    """Why this value cannot travel, and what the author can actually do about it.

    The two directions have different remedies, and offering the wrong one is worse than offering none.
    An output can be held, because the node that produced it runs in the process holding it. An input
    cannot: the object is here and the node is elsewhere, so there is nothing to hold it for and a key
    would name an entry the far side has no way to look up.
    """
    where = f"the value of parameter '{name}' on node '{node_name}'"
    cause = f"a '{type(value).__name__}' cannot be converted to data"
    if is_output and is_container:
        # ParameterContainer.is_process_local is always False -- the container is not itself the thing
        # held -- so telling the author to mark it would send them round the same loop.
        remedy = (
            "A list or dictionary parameter cannot be kept here as a whole. Output the value on an "
            "ordinary parameter marked serializable=False instead."
        )
    elif is_output:
        remedy = "Mark that parameter serializable=False so the value is kept here and passed by reference."
    else:
        remedy = (
            "It was built in this process while the node runs in another, so it cannot be passed by "
            "reference either. Have the node that produces it run in the same library as this one, or "
            "output a saved file instead."
        )
    return f"Attempted to send {where} to another process. Failed due to: {cause}. {remedy}"


def _is_sendable(value: Any) -> bool:
    """Whether `value` can be put on the wire at all, as the transport would.

    Deliberately looser than what parking asks. Parking is decided by `_is_json_safe`, which is "this is
    already data"; an author who declared the parameter unpersistable gets their object held rather than
    unstructured, because cattrs will happily turn any attrs class into a dict of its fields and an
    artifact a library defines itself comes out of that without its payload.

    This one is the transport's own question: `json.dumps` with `default=str`, so anything it can encode
    is sent rather than rejected, and only a value it would mangle into a repr earns the raise.
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
        # json.dumps coerces int/float/bool/None keys rather than refusing them, so a dict keyed by frame
        # number travels fine and must not be reported as unsendable.
        return all(isinstance(k, (str, int, float, bool)) or k is None for k in value) and all(
            _is_json_safe(v) for v in value.values()
        )
    return False
