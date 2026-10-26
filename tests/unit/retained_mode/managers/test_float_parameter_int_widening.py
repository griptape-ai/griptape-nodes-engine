"""A whole number set on a user-added float parameter is stored as a float.

The editor sends ``2.0`` as JSON ``2``, so a float parameter added from the editor receives an
``int`` for every whole-number default and value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsRequest,
    AlterParameterDetailsResultSuccess,
)
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _FLOW_NAME,
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _freeze_workflow_clock,
    _fresh_flow,
    _get_value,
    _reload_from_disk,
    _save_flow_to_disk,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.retained_mode.engine import Engine


def _node_with_user_parameter(
    engine: Engine,
    tmp_path: Path,
    parameter_type: str | None,
    default_value: Any,
    input_types: list[str] | None = None,
) -> str:
    flow_name, library_name = _fresh_flow(engine, "float_widening_workflow", tmp_path)
    node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
    added = engine.handle_request(
        AddParameterToNodeRequest(
            node_name=node_name,
            parameter_name="amount",
            type=parameter_type,
            input_types=input_types,
            default_value=default_value,
            tooltip="t",
            is_user_defined=True,
        )
    )
    assert isinstance(added, AddParameterToNodeResultSuccess), added
    return node_name


def _parameter(engine: Engine, node_name: str) -> Parameter:
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    assert node is not None
    parameter = node.get_parameter_by_name("amount")
    assert parameter is not None
    return parameter


def _assert_float(value: Any, expected: float) -> None:
    assert type(value) is float
    assert value == expected


class TestFloatParameterIntWidening:
    def test_added_default_is_stored_as_float(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "float", 2)

        _assert_float(_parameter(engine, node_name).default_value, 2.0)
        _assert_float(_get_value(engine, node_name, "amount"), 2.0)

    def test_altered_default_is_stored_as_float(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "float", 1.5)

        result = engine.handle_request(
            AlterParameterDetailsRequest(node_name=node_name, parameter_name="amount", default_value=3)
        )

        assert isinstance(result, AlterParameterDetailsResultSuccess), result
        _assert_float(_parameter(engine, node_name).default_value, 3.0)
        _assert_float(_get_value(engine, node_name, "amount"), 3.0)

    def test_default_altered_with_a_change_to_float_is_stored_as_float(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "str", "x")

        result = engine.handle_request(
            AlterParameterDetailsRequest(
                node_name=node_name, parameter_name="amount", type="float", input_types=["float"], default_value=3
            )
        )

        assert isinstance(result, AlterParameterDetailsResultSuccess), result
        _assert_float(_parameter(engine, node_name).default_value, 3.0)
        _assert_float(_get_value(engine, node_name, "amount"), 3.0)

    def test_change_to_float_widens_the_existing_value(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "int", 2)

        result = engine.handle_request(
            AlterParameterDetailsRequest(
                node_name=node_name, parameter_name="amount", type="float", input_types=["float"], output_type="float"
            )
        )

        assert isinstance(result, AlterParameterDetailsResultSuccess), result
        _assert_float(_parameter(engine, node_name).default_value, 2.0)
        _assert_float(_get_value(engine, node_name, "amount"), 2.0)

    def test_set_value_is_stored_as_float(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "float", 1.5)

        _set_value(engine, node_name, "amount", 4)

        _assert_float(_get_value(engine, node_name, "amount"), 4.0)

    def test_value_survives_save_and_reload_as_float(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "float", 2)
        _set_value(engine, node_name, "amount", 4)

        file_path = _save_flow_to_disk(engine, _FLOW_NAME, tmp_path, "float_widening")
        with pytest.MonkeyPatch.context() as monkeypatch:
            _freeze_workflow_clock(monkeypatch)
            _reload_from_disk(engine, file_path)

        _assert_float(_parameter(engine, node_name).default_value, 2.0)
        _assert_float(_get_value(engine, node_name, "amount"), 4.0)

    def test_bool_is_not_widened(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "float", True)

        assert _get_value(engine, node_name, "amount") is True

    def test_parameter_that_also_accepts_int_keeps_int(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, None, 2, input_types=["float", "int"])

        value = _get_value(engine, node_name, "amount")
        assert type(value) is int
        assert value == 2  # noqa: PLR2004

    def test_int_parameter_keeps_int(self, engine: Engine, tmp_path: Path) -> None:
        node_name = _node_with_user_parameter(engine, tmp_path, "int", 2)

        _set_value(engine, node_name, "amount", 4)

        value = _get_value(engine, node_name, "amount")
        assert type(value) is int
        assert value == 4  # noqa: PLR2004
