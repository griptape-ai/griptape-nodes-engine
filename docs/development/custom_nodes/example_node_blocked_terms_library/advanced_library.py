"""Advanced library that cancels a run when a node input contains a blocked term.

The engine registers the listener from `get_execution_event_listeners()` when this library loads
and removes it when it unloads. Listeners are notification-only, so this cannot stop a node from
starting. It asks the engine to cancel the flow while the node is starting, and the node may
already be running by the time the cancel lands.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.retained_mode.events.execution_events import CancelFlowRequest, NodeStartProcessEvent
from griptape_nodes.retained_mode.events.node_events import (
    ListParametersOnNodeRequest,
    ListParametersOnNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

logger = logging.getLogger("griptape_nodes")

# A run is cancelled when a text input contains one of its terms, ignoring case. Each key is a
# parameter name, so "prompt" only checks parameters called prompt, on any node that has one.
# The key "*" checks every text input on every node.
BLOCKED_TERMS_BY_PARAMETER = {
    "prompt": ("CONFIDENTIAL",),
}
ANY_PARAMETER = "*"


class BlockedTermsLibrary(AdvancedNodeLibrary):
    def get_execution_event_listeners(self) -> list[tuple[type, Callable]]:
        # NodeStartProcessEvent is sent once for every node the engine runs, just before the node
        # does any work. For a node inside a loop body the event names the canvas node, and that
        # node holds the values from before the loop started, not the ones the current iteration
        # is using. Reading it by name is only right for nodes outside loops.
        return [(NodeStartProcessEvent, self._on_start)]

    def _on_start(self, event: NodeStartProcessEvent) -> None:
        blocked_term = self._find_blocked_term(event.node_name)
        if blocked_term is None:
            return
        self._cancel_flow(event.node_name, event.flow_name, blocked_term)

    def _find_blocked_term(self, node_name: str) -> str | None:
        """Return the first blocked term found in the node's text inputs, or None."""
        listed = GriptapeNodes.handle_request(ListParametersOnNodeRequest(node_name=node_name))
        if not isinstance(listed, ListParametersOnNodeResultSuccess):
            return None

        for parameter_name in listed.parameter_names:
            terms = BLOCKED_TERMS_BY_PARAMETER.get(parameter_name, ()) + BLOCKED_TERMS_BY_PARAMETER.get(
                ANY_PARAMETER, ()
            )
            # Only read the parameters that have terms to check.
            if not terms:
                continue
            result = GriptapeNodes.handle_request(
                GetParameterValueRequest(parameter_name=parameter_name, node_name=node_name)
            )
            if not isinstance(result, GetParameterValueResultSuccess):
                continue
            if not isinstance(result.value, str):
                continue
            for term in terms:
                if term.lower() in result.value.lower():
                    return term
        return None

    def _cancel_flow(self, node_name: str, flow_name: str | None, blocked_term: str) -> None:
        if flow_name is None:
            logger.warning(
                "%s contains blocked term %r but its flow is unknown. Not cancelling.", node_name, blocked_term
            )
            return
        loop = GriptapeNodes.get_instance().event_manager.event_loop
        if loop is None:
            logger.warning(
                "%s contains blocked term %r but the engine has no event loop. Not cancelling.",
                node_name,
                blocked_term,
            )
            return
        # The cancel has to run on the engine's own event loop, because that is where the run's
        # tasks live. The synchronous handle_request runs the handler on a temporary loop instead
        # and fails with "attached to a different loop". run_coroutine_threadsafe schedules the
        # cancel on the engine loop and returns at once, so this listener does not wait for it.
        future = asyncio.run_coroutine_threadsafe(
            GriptapeNodes.ahandle_request(CancelFlowRequest(flow_name=flow_name)), loop
        )
        future.add_done_callback(lambda done: self._log_cancel_result(node_name, flow_name, blocked_term, done))

    def _log_cancel_result(self, node_name: str, flow_name: str, blocked_term: str, done: Future) -> None:
        if done.exception() is not None:
            logger.warning(
                "%s contains blocked term %r. Cancelling flow %s raised: %s",
                node_name,
                blocked_term,
                flow_name,
                done.exception(),
            )
            return
        result = done.result()
        logger.warning(
            "%s contains blocked term %r. Cancelled flow %s: %s",
            node_name,
            blocked_term,
            flow_name,
            type(result).__name__,
        )
