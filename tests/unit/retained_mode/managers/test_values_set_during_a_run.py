"""A value set while a node's body runs is still there on the next run, whichever way it was set.

A node can set a value on itself from inside its own body, by calling the setter or by sending
`SetParameterValueRequest`, and both are used to carry state to its next run. The produced store is
cleared before each run, so what makes that work is the authored copy the setter always writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode, aprocess_scope
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


def _node_running_its_body(engine: Engine, tmp_path: Path, workflow_name: str) -> BaseNode:
    flow_name, library_name = _fresh_flow(engine, workflow_name, tmp_path)
    node_name = _create_round_trip_node(engine, "Node", flow_name, library_name)
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    assert node is not None
    return node


def test_a_request_a_node_sends_on_itself_outlives_the_run(engine: Engine, tmp_path: Path) -> None:
    """The documented way for a node to carry state to its next run."""
    node = _node_running_its_body(engine, tmp_path, "request_state")

    with aprocess_scope(None, node):
        _set_value(engine, node.name, "value", "state for the next run")

    # What the resolution machinery does before the next run.
    node.parameter_output_values.silent_clear()
    assert node.get_parameter_value("value") == "state for the next run"


def test_a_direct_set_in_the_body_also_reports_the_result(engine: Engine, tmp_path: Path) -> None:
    """The same set has to do both: outlive the run, and travel back from a worker as a result."""
    node = _node_running_its_body(engine, tmp_path, "direct_state")

    with aprocess_scope(None, node):
        node.set_parameter_value("value", "what this run produced")

    assert node.parameter_output_values["value"] == "what this run produced"
    node.parameter_output_values.silent_clear()
    assert node.get_parameter_value("value") == "what this run produced"
