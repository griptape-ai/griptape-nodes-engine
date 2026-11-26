"""Which requests never change a workflow, as undo needs to know before they run."""

from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.events.arbitrary_python_events import RunArbitraryPythonStringRequest
from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, GetNodeMetadataRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.request_effects import never_alters_workflow


@dataclass
class _UnansweredRequest(RequestPayload):
    """A request with no results named after it."""


class TestNeverAltersWorkflow:
    def test_a_read_never_alters_the_workflow(self) -> None:
        assert never_alters_workflow(GetNodeMetadataRequest)

    def test_a_request_whose_handler_decides_may_alter_it(self) -> None:
        assert not never_alters_workflow(SetParameterValueRequest)

    def test_a_request_that_always_alters_it_may_alter_it(self) -> None:
        assert not never_alters_workflow(CreateNodeRequest)

    def test_a_request_whose_failure_reports_a_change_may_alter_it(self) -> None:
        """Running Python can change the workflow before it fails."""
        assert not never_alters_workflow(RunArbitraryPythonStringRequest)

    def test_a_request_without_results_named_after_it_may_alter_it(self) -> None:
        assert not never_alters_workflow(_UnansweredRequest)
