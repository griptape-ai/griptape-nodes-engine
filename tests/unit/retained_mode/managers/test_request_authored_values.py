"""A value that arrives through `SetParameterValueRequest` is authored, wherever it comes from.

A node can send the request on itself from inside its own body, which is how it carries state to its
next run. Storing that as something the run produced would lose it, because the produced store is
cleared before each run. A direct `set_parameter_value` call in the same place means the other thing:
that is the node reporting a result.
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


def test_a_request_a_node_sends_on_itself_authors_the_value(engine: Engine, tmp_path: Path) -> None:
    """The documented way to carry state across runs, so it has to outlive the run that set it."""
    node = _node_running_its_body(engine, tmp_path, "authored_request")

    with aprocess_scope(None, node):
        _set_value(engine, node.name, "value", "state for the next run")

    # What the resolution machinery does before the next run.
    node.parameter_output_values.silent_clear()
    assert node.get_parameter_value("value") == "state for the next run"


def test_a_direct_set_in_the_body_records_a_result(engine: Engine, tmp_path: Path) -> None:
    """The contrast, on the same parameter: this one is the node reporting what it computed."""
    node = _node_running_its_body(engine, tmp_path, "produced_direct")

    with aprocess_scope(None, node):
        node.set_parameter_value("value", "what this run produced")

    assert node.parameter_output_values["value"] == "what this run produced"
    assert "value" not in node.parameter_values
