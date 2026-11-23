"""A value set while a node's body runs is still there on the next run, whichever way it was set.

A node can set a value on itself from inside its own body, by calling the setter or by sending
`SetParameterValueRequest`, and both are used to carry state to its next run. The produced store is
cleared before each run, so what makes that work is the authored copy the setter always writes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode, aprocess_scope
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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


# A seed a node rolled while running.
_SEED = 7


class Handle:
    """Stands in for a value with no plain-data form."""


class TestWhatTheRunSet:
    """A node knows which of its values its own run set, so they can follow the run out of a worker."""

    def test_a_value_the_body_sets_on_its_node_is_the_runs(self, engine: Engine, tmp_path: Path) -> None:
        node = _node_running_its_body(engine, tmp_path, "run_set")

        with aprocess_scope(None, node):
            node.set_parameter_value("value", _SEED)

        assert node.values_set_by_run == {"value": _SEED}

    def test_an_edit_after_the_run_takes_it_back(self, engine: Engine, tmp_path: Path) -> None:
        node = _node_running_its_body(engine, tmp_path, "edited")
        with aprocess_scope(None, node):
            node.set_parameter_value("value", _SEED)

        _set_value(engine, node.name, "value", _SEED + 1)

        assert node.values_set_by_run == {}

    def test_a_value_another_running_node_sets_is_not_the_recipients(self, engine: Engine, tmp_path: Path) -> None:
        """How a value reaches a connected input. It belongs to the connection, not to the recipient's run."""
        flow_name, library_name = _fresh_flow(engine, "fed", tmp_path)
        nodes = [
            engine.object_manager.attempt_get_object_by_name_as_type(
                _create_round_trip_node(engine, name, flow_name, library_name), BaseNode
            )
            for name in ("Upstream", "Downstream")
        ]
        upstream, node = nodes
        assert upstream is not None
        assert node is not None

        with aprocess_scope(None, upstream):
            node.set_parameter_value("value", _SEED)

        assert node.values_set_by_run == {}

    def test_values_a_run_set_elsewhere_are_taken_as_the_runs(self, engine: Engine, tmp_path: Path) -> None:
        node = _node_running_its_body(engine, tmp_path, "taken")

        node.take_values_set_by_run({"value": _SEED})

        assert node.get_parameter_value("value") == _SEED
        assert node.values_set_by_run == {"value": _SEED}


class TestWhatAWorkerSendsBack:
    def test_the_values_its_run_set(self, engine: Engine, tmp_path: Path) -> None:
        node = _node_running_its_body(engine, tmp_path, "worker_sends")
        with aprocess_scope(None, node):
            node.set_parameter_value("value", _SEED)

        result = engine.node_manager._worker_execution_result(node)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert result.parameter_values_set_by_run == {"value": _SEED}

    def test_not_a_value_with_no_plain_data_form(
        self, engine: Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Inputs are not cached for egress, so such a value stays in the worker, with a warning."""
        node = _node_running_its_body(engine, tmp_path, "worker_keeps")
        with aprocess_scope(None, node):
            node.set_parameter_value("value2", Handle())
            node.parameter_output_values.silent_clear()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = engine.node_manager._worker_execution_result(node)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert result.parameter_values_set_by_run == {}
        assert "parameter 'value2'" in caplog.text
