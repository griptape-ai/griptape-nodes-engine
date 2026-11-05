"""Outputs a node recomputes in `after_value_set` reach connected inputs at edit time only.

Live-preview nodes render in `after_value_set`. At edit time that preview is pushed to connected
downstream inputs so the rest of the graph reflects it. During a run every downstream node collects
its inputs from upstream outputs before it executes, so pushing previews ahead of that re-fires each
downstream `after_value_set` and a chain re-renders once per upstream hop.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
    _get_value,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.retained_mode.engine import Engine

CHAIN = ("First", "Second", "Third")


def _node(engine: Engine, node_name: str) -> BaseNode:
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    assert node is not None
    return node


def _preview_chain(engine: Engine, tmp_path: Path, workflow_name: str) -> dict[str, list[Any]]:
    """Build First -> Second -> Third where each node previews `value` into its `value2` output.

    Returns the values each node rendered a preview for, in order.
    """
    flow_name, library_name = _fresh_flow(engine, workflow_name, tmp_path)
    renders: dict[str, list[Any]] = {}
    for node_name in CHAIN:
        _create_round_trip_node(engine, node_name, flow_name, library_name)
        node = _node(engine, node_name)
        renders[node_name] = []

        def render_preview(parameter: Parameter, value: Any, node: BaseNode = node) -> None:
            if parameter.name != "value":
                return
            renders[node.name].append(value)
            node.parameter_output_values["value2"] = f"preview({value})"

        node.after_value_set = render_preview

    for source_name, target_name in itertools.pairwise(CHAIN):
        result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_name,
                source_parameter_name="value2",
                target_node_name=target_name,
                target_parameter_name="value",
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result
    # Connecting passes the source's current value through, which is not under test.
    for node_renders in renders.values():
        node_renders.clear()
    return renders


class TestEditTime:
    def test_preview_output_reaches_every_downstream_node(self, engine: Engine, tmp_path: Path) -> None:
        renders = _preview_chain(engine, tmp_path, "edit_time_preview")

        _set_value(engine, "First", "value", "img")

        assert _get_value(engine, "Second", "value") == "preview(img)"
        assert _get_value(engine, "Third", "value") == "preview(preview(img))"
        assert renders == {"First": ["img"], "Second": ["preview(img)"], "Third": ["preview(preview(img))"]}


class TestDuringRun:
    def test_preview_output_stays_on_the_node_that_rendered_it(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        renders = _preview_chain(engine, tmp_path, "run_time_preview")
        monkeypatch.setattr(engine.flow_manager, "check_for_existing_running_flow", lambda: True)

        _set_value(engine, "First", "value", "img")

        assert _node(engine, "First").parameter_output_values["value2"] == "preview(img)"
        assert _get_value(engine, "Second", "value") is None
        assert renders == {"First": ["img"], "Second": [], "Third": []}

    def test_direct_value_still_reaches_connected_inputs(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only recomputed outputs are held back; the value set on a parameter still passes through its own connections."""
        flow_name, library_name = _fresh_flow(engine, "run_time_direct", tmp_path)
        _create_round_trip_node(engine, "Source", flow_name, library_name)
        _create_round_trip_node(engine, "Target", flow_name, library_name)
        result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="value",
                target_node_name="Target",
                target_parameter_name="value",
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result
        monkeypatch.setattr(engine.flow_manager, "check_for_existing_running_flow", lambda: True)

        _set_value(engine, "Source", "value", "passed")

        assert _get_value(engine, "Target", "value") == "passed"
