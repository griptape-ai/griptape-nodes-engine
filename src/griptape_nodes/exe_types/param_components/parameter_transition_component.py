import dataclasses
import logging
from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    IncomingConnection,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    GetConnectionsForParameterRequest,
    GetConnectionsForParameterResultSuccess,
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")


@dataclasses.dataclass(frozen=True)
class TransitionParameter:
    """Describes a parameter the caller wants to exist after a transition.

    When a same-named Parameter already exists on the node, the component
    compares signatures: identical signatures are a no-op; any difference
    triggers a clean replace — capture existing connections, remove the
    old Parameter, add the new one, then re-create each captured
    connection the platform still considers type-compatible.

    Type-signature fields describe the values the caller expects to read
    off the created Parameter's public `input_types` and `output_type`
    properties. Those properties apply fallbacks when a field is unset —
    e.g., an input-only Parameter's `output_type` falls back to its first
    input type — so populate these fields accordingly.
    """

    name: str
    allowed_modes: frozenset[ParameterMode]
    input_types: frozenset[str]
    output_type: str
    add_request_factory: Callable[[], AddParameterToNodeRequest]


@dataclasses.dataclass(frozen=True)
class TransitionPlan:
    """Computed diff of current vs. desired parameters.

    `to_preserve` covers parameters where no action was needed: the current
    Parameter's signature already matches the desired TransitionParameter.
    `to_replace` covers same-named parameters whose signature differed; the
    component replaces them cleanly, best-effort restoring compatible
    connections. `to_remove` and `to_add` cover the asymmetric cases.
    """

    to_preserve: frozenset[str]
    to_replace: frozenset[str]
    to_remove: frozenset[str]
    to_add: frozenset[str]


def _signatures_match(current: Parameter, desired: TransitionParameter) -> bool:
    """True when a Parameter already matches a desired TransitionParameter."""
    if frozenset(current.allowed_modes) != desired.allowed_modes:
        return False
    if frozenset(current.input_types) != desired.input_types:
        return False
    return current.output_type == desired.output_type


# TODO: https://github.com/griptape-ai/griptape-nodes-engine/issues/4793 — support
# nested ParameterGroup lifecycle and parent-driven identity. This component
# currently manages flat Parameter lists only. Callers needing grouped or
# nested-grouped dynamic parameters must manage ParameterGroup creation and
# cleanup outside the component (see the openassetio library for the established
# workaround pattern). Two distinct gaps tracked together:
#   1. ParameterGroup lifecycle is not part of the transition (no engine event
#      exists today to remove a ParameterGroup).
#   2. Parent membership is not part of identity comparison, so a parameter
#      "moved" between groups (same name + signature, different parent) is
#      silently treated as preserved instead of relocated.
class ParameterTransitionComponent:
    """Reusable component for diff-based parameter schema transitions.

    Given a set of parameters the caller wants to exist, this component
    reconciles the node's current parameter surface against that
    description. For same-named parameters, matching signatures are left
    alone; mismatching signatures are replaced cleanly — the old Parameter
    is removed, a fresh one is added, and each connection the old
    parameter had is re-dispatched through the standard
    `CreateConnectionRequest` path so connections that are still
    type-compatible survive (and their values re-flow via the normal
    connection mechanism). Parameters that no longer exist in the desired
    set are removed; genuinely new parameters are added.

    Compatibility contract:
        The public API of this component is extensible. Future enhancements
        will append new fields to ``TransitionParameter`` and
        ``TransitionPlan`` with defaults, add new keyword-only arguments to
        ``transition_to`` and ``compute_plan`` with defaults, and preserve
        the zero-arg shape of ``add_request_factory``. Existing field types
        and bucket names will not change in incompatible ways. Callers
        building on this component today can rely on these guarantees when
        nested-group support lands in a follow-up.
    """

    def __init__(
        self,
        node: Any,
        *,
        manages_parameter: Callable[[Parameter], bool],
    ) -> None:
        """Create a component bound to a node.

        Args:
            node: The node instance whose parameter surface this component manages.
            manages_parameter: Predicate identifying which of the node's parameters
                belong to this component's "current" set. Everything outside the
                predicate is left alone.
        """
        self._node = node
        self._manages_parameter = manages_parameter

    def transition_to(self, desired: list[TransitionParameter]) -> TransitionPlan:
        """Compute and apply a transition to the desired parameter set.

        Returns the computed plan describing what action was taken per name.
        """
        plan = self.compute_plan(desired)
        desired_by_name = {param.name: param for param in desired}
        self._apply_plan(plan, desired_by_name)
        return plan

    def compute_plan(self, desired: list[TransitionParameter]) -> TransitionPlan:
        """Compute the transition plan without applying it.

        Raises:
            ValueError: If `desired` contains two TransitionParameters with the same name.
        """
        current_by_name: dict[str, Parameter] = {}
        for param in self._node.parameters:
            if self._manages_parameter(param):
                current_by_name[param.name] = param

        desired_by_name: dict[str, TransitionParameter] = {}
        for desired_param in desired:
            if desired_param.name in desired_by_name:
                msg = (
                    f"Attempted to compute parameter transition plan for node "
                    f"'{self._node.name}'. Failed because TransitionParameter name "
                    f"'{desired_param.name}' appears twice in the desired list."
                )
                raise ValueError(msg)
            desired_by_name[desired_param.name] = desired_param

        current_names = set(current_by_name.keys())
        desired_names = set(desired_by_name.keys())

        to_preserve: set[str] = set()
        to_replace: set[str] = set()

        for name in current_names & desired_names:
            if _signatures_match(current_by_name[name], desired_by_name[name]):
                to_preserve.add(name)
            else:
                to_replace.add(name)

        return TransitionPlan(
            to_preserve=frozenset(to_preserve),
            to_replace=frozenset(to_replace),
            to_remove=frozenset(current_names - desired_names),
            to_add=frozenset(desired_names - current_names),
        )

    def _apply_plan(self, plan: TransitionPlan, desired_by_name: dict[str, TransitionParameter]) -> None:
        """Dispatch event requests to realize a computed plan.

        Order: replaces first (so their freed names don't collide with anything),
        then removes, then adds. Preserved parameters require no action.
        """
        for name in plan.to_replace:
            self._replace_parameter(name, desired_by_name[name])

        for name in plan.to_remove:
            self._remove_parameter(name)

        for name in plan.to_add:
            self._add_parameter(desired_by_name[name])

    def _replace_parameter(self, name: str, desired: TransitionParameter) -> None:
        """Capture connections, remove the old parameter, add the new one, re-create connections."""
        captured = self._capture_connections(name)

        if not self._remove_parameter(name):
            return

        if not self._add_parameter(desired):
            return

        self._recreate_connections(captured)

    def _capture_connections(self, parameter_name: str) -> GetConnectionsForParameterResultSuccess | None:
        """Fetch the connection lists for a parameter, or None if the query fails."""
        request = GetConnectionsForParameterRequest(parameter_name=parameter_name, node_name=self._node.name)
        result = GriptapeNodes.handle_request(request)
        if isinstance(result, GetConnectionsForParameterResultSuccess):
            return result
        logger.warning(
            "Could not capture connections for parameter '%s' on node '%s' before replace; "
            "any connections on that parameter will be lost.",
            parameter_name,
            self._node.name,
        )
        return None

    def _remove_parameter(self, name: str) -> bool:
        """Dispatch RemoveParameterFromNodeRequest. Returns True on success."""
        request = RemoveParameterFromNodeRequest(parameter_name=name, node_name=self._node.name)
        result = GriptapeNodes.handle_request(request)
        if result.failed():
            logger.error(
                "Attempted to remove parameter '%s' from node '%s'. Failed because: %s",
                name,
                self._node.name,
                result.result_details,
            )
            return False
        return True

    def _add_parameter(self, desired: TransitionParameter) -> bool:
        """Dispatch the caller-supplied add-request factory. Returns True on success."""
        add_request = desired.add_request_factory()
        result = GriptapeNodes.handle_request(add_request)
        if result.failed():
            logger.error(
                "Attempted to add parameter '%s' to node '%s'. Failed because: %s",
                desired.name,
                self._node.name,
                result.result_details,
            )
            return False
        return True

    def _recreate_connections(self, captured: GetConnectionsForParameterResultSuccess | None) -> None:
        """Re-dispatch CreateConnectionRequest for every captured edge.

        The connection handler validates type compatibility and rejects edges
        that are no longer valid under the new parameter's signature; we log
        each rejection at debug level because it reflects a legitimate schema
        change, not an error.
        """
        if captured is None:
            return

        for incoming in captured.incoming_connections:
            self._recreate_incoming_connection(incoming)

        for outgoing in captured.outgoing_connections:
            self._recreate_outgoing_connection(outgoing)

    def _recreate_incoming_connection(self, incoming: IncomingConnection) -> None:
        request = CreateConnectionRequest(
            source_node_name=incoming.source_node_name,
            source_parameter_name=incoming.source_parameter_name,
            target_node_name=self._node.name,
            target_parameter_name=incoming.target_parameter_name,
        )
        result = GriptapeNodes.handle_request(request)
        if result.failed():
            logger.debug(
                "Dropped incoming connection %s.%s -> %s.%s during replace because: %s",
                incoming.source_node_name,
                incoming.source_parameter_name,
                self._node.name,
                incoming.target_parameter_name,
                result.result_details,
            )

    def _recreate_outgoing_connection(self, outgoing: OutgoingConnection) -> None:
        request = CreateConnectionRequest(
            source_node_name=self._node.name,
            source_parameter_name=outgoing.source_parameter_name,
            target_node_name=outgoing.target_node_name,
            target_parameter_name=outgoing.target_parameter_name,
        )
        result = GriptapeNodes.handle_request(request)
        if result.failed():
            logger.debug(
                "Dropped outgoing connection %s.%s -> %s.%s during replace because: %s",
                self._node.name,
                outgoing.source_parameter_name,
                outgoing.target_node_name,
                outgoing.target_parameter_name,
                result.result_details,
            )
