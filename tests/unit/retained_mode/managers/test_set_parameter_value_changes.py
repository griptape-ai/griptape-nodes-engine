"""Setting a parameter reports a change to the workflow only when the parameter's value changed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


def _set(engine: Engine, node_name: str, value: str) -> SetParameterValueResultSuccess:
    result = engine.handle_request(SetParameterValueRequest(node_name=node_name, parameter_name="value", value=value))
    assert isinstance(result, SetParameterValueResultSuccess), result
    return result


class TestSettingAValue:
    def test_a_new_value_is_a_change(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "new_value", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "first")

        assert _set(engine, node_name, "second").altered_workflow_state

    def test_the_value_it_has_is_no_change(self, engine: Engine, tmp_path: Path) -> None:
        """Such as an editor field sending its value back when it loses focus."""
        flow_name, library_name = _fresh_flow(engine, "same_value", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "first")

        assert not _set(engine, node_name, "first").altered_workflow_state
