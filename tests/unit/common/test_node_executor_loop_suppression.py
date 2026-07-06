"""Contract tests for the event-suppression sets used during loop execution.

Parallel iterative groups deserialize one full flow instance per iteration,
run them, then delete them. Every request issued during that deserialize /
set-value / delete cascade produces a result event that, if broadcast, floods
the editor with updates for iteration flows it never learned about and later
cannot resolve (surfacing as "no such Flow was found" metadata errors and, at
volume, engine disconnects).

These tests pin the membership of the suppression sets so that the cascade
stays silent. They are intentionally coarse membership checks rather than
behavioral tests because the flood only manifests with a live event queue.
"""

from griptape_nodes.common.node_executor import (
    LOOP_CLEANUP_EVENTS_TO_SUPPRESS,
    LOOP_EVENTS_TO_SUPPRESS,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionResultSuccess,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowResultSuccess,
    DeleteFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeResultSuccess,
    DeleteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsResultSuccess,
    SetParameterValueResultSuccess,
)


class TestLoopEventsToSuppress:
    """The deserialize/build cascade for each iteration flow must stay silent."""

    def test_suppresses_node_creation_results(self) -> None:
        # Deserializing an iteration flow creates a node per packaged node, per
        # iteration. Without suppression these are the highest-volume leak.
        assert CreateNodeResultSuccess in LOOP_EVENTS_TO_SUPPRESS

    def test_suppresses_parameter_element_results(self) -> None:
        # Deserialized nodes re-add their parameters and alter their details.
        assert AddParameterToNodeResultSuccess in LOOP_EVENTS_TO_SUPPRESS
        assert AlterParameterDetailsResultSuccess in LOOP_EVENTS_TO_SUPPRESS

    def test_suppresses_flow_connection_and_value_results(self) -> None:
        assert CreateFlowResultSuccess in LOOP_EVENTS_TO_SUPPRESS
        assert CreateConnectionResultSuccess in LOOP_EVENTS_TO_SUPPRESS
        assert SetParameterValueResultSuccess in LOOP_EVENTS_TO_SUPPRESS


class TestLoopCleanupEventsToSuppress:
    """Tearing down iteration flows cascades into node/connection deletes."""

    def test_suppresses_flow_deletion(self) -> None:
        assert DeleteFlowResultSuccess in LOOP_CLEANUP_EVENTS_TO_SUPPRESS

    def test_suppresses_cascaded_node_and_connection_deletion(self) -> None:
        # DeleteFlowRequest deletes child nodes and their connections; those
        # result events reference flows the editor is about to lose.
        assert DeleteNodeResultSuccess in LOOP_CLEANUP_EVENTS_TO_SUPPRESS
        assert DeleteConnectionResultSuccess in LOOP_CLEANUP_EVENTS_TO_SUPPRESS
