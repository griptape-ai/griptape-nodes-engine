"""A value a parameter's converter rejects reaches the editor as the converter's message alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_types.parameter_yaml import ParameterYaml
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultFailure,
)
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


def test_rejected_yaml_reports_only_the_yaml_message(engine: Engine, tmp_path: Path) -> None:
    """The editor shows this text under a "SetParameterValue Failed" title, so it must not repeat it."""
    flow_name, library_name = _fresh_flow(engine, "invalid_yaml", tmp_path)
    node_name = _create_round_trip_node(engine, "YAML Input", flow_name, library_name)
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    assert node is not None
    node.add_parameter(ParameterYaml(name="yaml"))

    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name="yaml", value="hi: hello\nhi2: there: here\n")
    )

    assert isinstance(result, SetParameterValueResultFailure), result
    assert str(result.result_details).startswith(
        "Couldn't set 'YAML Input.yaml'.\n\nInvalid YAML on line 2: hi2: there: here\n\n"
        "A colon followed by a space starts a new key."
    )
