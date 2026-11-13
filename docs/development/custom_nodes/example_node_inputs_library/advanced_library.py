"""Advanced library that logs the input parameters of every node run, including model and prompt.

The engine registers the listener from `get_execution_event_listeners()` when this library loads
and removes it when it unloads. Each time a node starts, the listener reads the node's input
parameters and writes them as one JSON object on one log line.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.retained_mode.events.execution_events import NodeStartProcessEvent
from griptape_nodes.retained_mode.events.node_events import (
    ListParametersOnNodeRequest,
    ListParametersOnNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterDetailsRequest,
    GetParameterDetailsResultSuccess,
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.node_library.library_registry import Library, LibrarySchema

logger = logging.getLogger("griptape_nodes")

# Long values, such as prompts, are cut to this many characters in the log line.
MAX_VALUE_CHARS = 500


class NodeInputsLibrary(AdvancedNodeLibrary):
    def __init__(self) -> None:
        # Listeners should return quickly, so the log line is written from a background thread.
        # This is where you would send the inputs to your own monitoring instead.
        self._writer = ThreadPoolExecutor(max_workers=1)

    def get_execution_event_listeners(self) -> list[tuple[type, Callable]]:
        # NodeStartProcessEvent is sent once for every node the engine runs, just before the node
        # does any work. For a node inside a loop body the event names the canvas node, and that
        # node holds the values from before the loop started, not the ones the current iteration
        # is using. Reading it by name is only right for nodes outside loops.
        return [(NodeStartProcessEvent, self._on_start)]

    def before_library_unregistered(self, library_data: LibrarySchema, library: Library) -> None:  # noqa: ARG002
        # Lets any log lines still queued finish before the library goes away.
        self._writer.shutdown(wait=True)

    def _on_start(self, event: NodeStartProcessEvent) -> None:
        values = self._collect_inputs(event.node_name)
        # A node with no input parameters has nothing to report.
        if not values:
            return
        record = {
            "node_name": event.node_name,
            "node_type": event.node_type,
            # Matches the execution_id on the NodeFinishProcessEvent for the same run.
            "execution_id": event.execution_id,
            "inputs": {name: self._summarize(value) for name, value in values.items()},
        }
        # default=str keeps json.dumps from failing on a value it does not know how to encode.
        self._writer.submit(logger.info, "%s", json.dumps(record, default=str))

    def _collect_inputs(self, node_name: str) -> dict[str, Any]:
        """Return {parameter name: value} for the node's input and property parameters.

        Requests go through `GriptapeNodes.handle_request` because a library should not reach into
        the engine's managers directly. A request that fails is skipped rather than raised, so a
        node the engine can't read never breaks the run it is logging.
        """
        listed = GriptapeNodes.handle_request(ListParametersOnNodeRequest(node_name=node_name))
        if not isinstance(listed, ListParametersOnNodeResultSuccess):
            return {}

        values: dict[str, Any] = {}
        for parameter_name in listed.parameter_names:
            if not self._is_input(node_name, parameter_name):
                continue
            result = GriptapeNodes.handle_request(
                GetParameterValueRequest(parameter_name=parameter_name, node_name=node_name)
            )
            if not isinstance(result, GetParameterValueResultSuccess):
                continue
            values[parameter_name] = result.value
        return values

    def _is_input(self, node_name: str, parameter_name: str) -> bool:
        """Whether the parameter takes a value from the user or from an upstream node.

        Output-only parameters hold results, not inputs, and private parameters are internal to the
        node. Both are left out.
        """
        details = GriptapeNodes.handle_request(
            GetParameterDetailsRequest(parameter_name=parameter_name, node_name=node_name)
        )
        if not isinstance(details, GetParameterDetailsResultSuccess):
            return False
        if details.private:
            return False
        return details.mode_allowed_input or details.mode_allowed_property

    def _summarize(self, value: Any) -> Any:
        """Turn a parameter value into something short that is safe to put in JSON."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) <= MAX_VALUE_CHARS:
                return value
            return f"{value[:MAX_VALUE_CHARS]}... ({len(value)} characters)"
        # Images, audio, drivers and other objects: report the type, not the contents.
        return f"<{type(value).__name__}>"
