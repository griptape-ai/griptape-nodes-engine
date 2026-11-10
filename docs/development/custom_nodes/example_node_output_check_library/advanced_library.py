"""Advanced library that warns when a node's output contains a flagged term.

The engine registers the listener from `get_execution_event_listeners()` when this library loads
and removes it when it unloads. A `NodeFinishProcessEvent` names the node but carries none of its
output values, so the listener reads them with requests once the node has finished.

The warning names the node, the parameter and the term. It leaves the text itself out of the log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.retained_mode.events.execution_events import NodeFinishProcessEvent, NodeProcessOutcome
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

logger = logging.getLogger("griptape_nodes")

# An output is flagged when a text value contains one of its terms, ignoring case. Each key is a
# parameter name, so "output" only checks parameters called output, on any node that has one.
# The key "*" checks every text parameter on every node.
FLAGGED_TERMS_BY_PARAMETER = {
    "output": ("CONFIDENTIAL",),
}
ANY_PARAMETER = "*"


class OutputCheckLibrary(AdvancedNodeLibrary):
    def get_execution_event_listeners(self) -> list[tuple[type, Callable]]:
        # NodeFinishProcessEvent is sent once for every run that sent a NodeStartProcessEvent,
        # after the node's work is over. For a node inside a loop body the event names the canvas
        # node, and that node only gets the last iteration's outputs once the whole loop is done.
        # Reading it by name is only right for nodes outside loops.
        return [(NodeFinishProcessEvent, self._on_finish)]

    def _on_finish(self, event: NodeFinishProcessEvent) -> None:
        # A failed or cancelled node has no finished output to check.
        if event.outcome != NodeProcessOutcome.SUCCEEDED:
            return

        listed = GriptapeNodes.handle_request(ListParametersOnNodeRequest(node_name=event.node_name))
        if not isinstance(listed, ListParametersOnNodeResultSuccess):
            return

        for parameter_name in listed.parameter_names:
            terms = FLAGGED_TERMS_BY_PARAMETER.get(parameter_name, ()) + FLAGGED_TERMS_BY_PARAMETER.get(
                ANY_PARAMETER, ()
            )
            # Only read the parameters that have terms to check.
            if not terms:
                continue
            result = GriptapeNodes.handle_request(
                GetParameterValueRequest(parameter_name=parameter_name, node_name=event.node_name)
            )
            if not isinstance(result, GetParameterValueResultSuccess):
                continue
            if not isinstance(result.value, str):
                continue
            for term in terms:
                if term.lower() in result.value.lower():
                    # execution_id matches the one on the node's NodeStartProcessEvent.
                    logger.warning(
                        "%s (%s) output %r contains flagged term %r. execution_id=%s",
                        event.node_name,
                        event.node_type,
                        parameter_name,
                        term,
                        event.execution_id,
                    )
