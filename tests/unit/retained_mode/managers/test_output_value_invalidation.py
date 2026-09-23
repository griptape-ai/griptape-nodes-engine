"""Editing a parameter must invalidate the output value recorded under the same name.

A PROPERTY+OUTPUT parameter stores the typed value and the produced value under one name, and reads
prefer the produced one. Without invalidation an edit is masked by the value from before it, and
appears to revert when the workflow is reopened or the page is refreshed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.local_objects import make_reference
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    RenameParameterRequest,
    RenameParameterResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from tests.unit.retained_mode.managers.test_workflow_save_load_roundtrip import (
    _clear_library_registry_state,  # noqa: F401  -- autouse fixture, needed in this module too
    _create_round_trip_node,
    _fresh_flow,
    _FrozenDateTime,
    _get_value,
    _reload_from_disk,
    _save_flow_to_disk,
    _set_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


class _ArrayLike:
    """Stands in for a numpy array or dataframe: ``!=`` answers element-wise, not with a bool."""

    def __init__(self, items: list[int]) -> None:
        self.items = items

    def __eq__(self, other: object) -> Any:
        if not isinstance(other, _ArrayLike):
            return NotImplemented
        return _ArrayLike([mine == theirs for mine, theirs in zip(self.items, other.items, strict=False)])

    def __ne__(self, other: object) -> Any:
        if not isinstance(other, _ArrayLike):
            return NotImplemented
        return _ArrayLike([mine != theirs for mine, theirs in zip(self.items, other.items, strict=False)])

    def __bool__(self) -> bool:
        msg = "The truth value of an array with more than one element is ambiguous"
        raise ValueError(msg)

    __hash__ = None  # type: ignore[assignment]


def _record_output_value(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    """Stand in for "the node ran once and produced this output value"."""
    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value, is_output=True)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _node(engine: Engine, node_name: str) -> BaseNode:
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    assert node is not None
    return node


def _ran_once(engine: Engine, tmp_path: Path, workflow_name: str) -> tuple[str, str, str]:
    """Build a flow with one node that has a set value and a matching recorded output value."""
    flow_name, library_name = _fresh_flow(engine, workflow_name, tmp_path)
    node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
    _set_value(engine, node_name, "value", "original")
    _record_output_value(engine, node_name, "value", "original")
    return flow_name, library_name, node_name


class TestEditInvalidatesOutputValue:
    def test_edit_after_a_run_wins_the_read(self, engine: Engine, tmp_path: Path) -> None:
        """The core defect. Asserted before any save, which is where it actually appears."""
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "edit_wins")

        _set_value(engine, node_name, "value", "edited")

        assert _get_value(engine, node_name, "value") == "edited"
        assert "value" not in _node(engine, node_name).parameter_output_values

    def test_edit_after_a_run_survives_save_and_reload(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "edit_round_trip")
        _set_value(engine, node_name, "value", "edited")

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "edit_round_trip")
        with pytest.MonkeyPatch.context() as monkeypatch:
            import griptape_nodes.retained_mode.managers.workflow_manager as workflow_manager_module

            monkeypatch.setattr(workflow_manager_module, "datetime", _FrozenDateTime)
            _reload_from_disk(engine, file_path)

        assert _get_value(engine, node_name, "value") == "edited"

    def test_edit_without_a_prior_run_survives(self, engine: Engine, tmp_path: Path) -> None:
        """Control arm: no output value exists, so there is nothing to shadow the edit."""
        flow_name, library_name = _fresh_flow(engine, "no_prior_run", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)

        _set_value(engine, node_name, "value", "original")
        _set_value(engine, node_name, "value", "edited")

        assert _get_value(engine, node_name, "value") == "edited"

    def test_computed_output_value_is_discarded_not_overwritten(self, engine: Engine, tmp_path: Path) -> None:
        """Most such parameters compute their output; mirroring the edit would publish a value the node never produced."""
        flow_name, library_name = _fresh_flow(engine, "computed_output", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "12")
        _record_output_value(engine, node_name, "value", 12)  # the node parsed the text it was given

        _set_value(engine, node_name, "value", "99")

        output_values = _node(engine, node_name).parameter_output_values
        assert "value" not in output_values, "the edit must not be fabricated as a computed output"
        assert _get_value(engine, node_name, "value") == "99"


class TestInvalidationIsNarrow:
    def test_other_output_values_are_untouched(self, engine: Engine, tmp_path: Path) -> None:
        """Only the edited parameter's output value is stale; the rest still reflect the last run."""
        flow_name, library_name = _fresh_flow(engine, "narrow_scope", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "original")
        _record_output_value(engine, node_name, "value", "original")
        _record_output_value(engine, node_name, "value2", "sibling result")

        _set_value(engine, node_name, "value", "edited")

        output_values = _node(engine, node_name).parameter_output_values
        assert "value" not in output_values
        assert output_values["value2"] == "sibling result"

    def test_output_value_recomputed_during_the_set_is_kept(self, engine: Engine, tmp_path: Path) -> None:
        """Nodes that refresh their own output value in ``after_value_set`` produce the newer value.

        The recomputed value differs from the previous one here, which is how the guard detects the
        refresh. See ``test_equal_value_recompute_is_not_yet_detected`` for the case where it does not.
        """
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "hook_refresh")
        node = _node(engine, node_name)

        def refresh_output(parameter: Parameter, value: Any) -> None:  # noqa: ARG001
            node.parameter_output_values["value"] = "recomputed"

        node.after_value_set = refresh_output

        _set_value(engine, node_name, "value", "edited")

        assert node.parameter_output_values["value"] == "recomputed"
        assert _get_value(engine, node_name, "value") == "recomputed"

    @pytest.mark.xfail(
        reason="Known limitation: the guard infers 'a hook recomputed this' from the value changing, "
        "so a recompute landing on an equal value -- or an in-place mutation of a list or dict output "
        "-- reads as no recompute and the fresh output is discarded. No node in the libraries or "
        "engine has this shape today, and the consequence is display-only on an already-unresolved "
        "node.",
        strict=True,
    )
    def test_equal_value_recompute_is_not_yet_detected(self, engine: Engine, tmp_path: Path) -> None:
        """A hook recomputing the output to a value equal to the previous one looks like no hook ran."""
        flow_name, library_name = _fresh_flow(engine, "equal_recompute", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        recomputed_length = len("xyz")  # equal to len("abc"), so the guard sees no change
        _set_value(engine, node_name, "value", "abc")
        _record_output_value(engine, node_name, "value", recomputed_length)
        node = _node(engine, node_name)

        def recompute_length(parameter: Parameter, value: Any) -> None:
            if parameter.name == "value":
                node.parameter_output_values["value"] = len(value)

        node.after_value_set = recompute_length

        _set_value(engine, node_name, "value", "xyz")

        assert node.parameter_output_values["value"] == recomputed_length


class TestUpstreamPropagation:
    def test_a_propagated_value_also_discards_the_stale_output(self, engine: Engine, tmp_path: Path) -> None:
        """An arriving connection value is a set like any other, and the recorded output predates it."""
        flow_name, library_name = _fresh_flow(engine, "propagation", tmp_path)
        source_name = _create_round_trip_node(engine, "Source", flow_name, library_name)
        target_name = _create_round_trip_node(engine, "Target", flow_name, library_name)
        _set_value(engine, target_name, "value", "before")
        _record_output_value(engine, target_name, "value", "computed from before")

        result = engine.handle_request(
            SetParameterValueRequest(
                node_name=target_name,
                parameter_name="value",
                value="delivered upstream",
                incoming_connection_source_node_name=source_name,
                incoming_connection_source_parameter_name="value",
            )
        )
        assert isinstance(result, SetParameterValueResultSuccess), result

        assert "value" not in _node(engine, target_name).parameter_output_values
        assert _get_value(engine, target_name, "value") == "delivered upstream"


class TestInvalidationBoundaries:
    def test_a_set_marked_initial_setup_keeps_the_output_value(self, engine: Engine, tmp_path: Path) -> None:
        """The gate's condition, at the request level. ``TestLoadKeepsOutputValues`` covers the real load."""
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "initial_setup")

        result = engine.handle_request(
            SetParameterValueRequest(node_name=node_name, parameter_name="value", value="edited", initial_setup=True)
        )
        assert isinstance(result, SetParameterValueResultSuccess), result

        assert _node(engine, node_name).parameter_output_values["value"] == "original"

    def test_writing_an_output_value_keeps_it(self, engine: Engine, tmp_path: Path) -> None:
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "is_output")

        _record_output_value(engine, node_name, "value", "produced")

        assert _node(engine, node_name).parameter_output_values["value"] == "produced"

    def test_setting_the_same_value_keeps_the_output_value(self, engine: Engine, tmp_path: Path) -> None:
        """Nothing was edited, so nothing is stale."""
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "no_op_set")

        _set_value(engine, node_name, "value", "original")

        assert _node(engine, node_name).parameter_output_values["value"] == "original"


class TestLoadKeepsOutputValues:
    """Loading a saved workflow restores output values, so nothing it replays may discard one.

    These go through the real chain -- serialize, write, exec the file's ``build_workflow()`` -- rather
    than firing a request with ``initial_setup=True`` by hand, so they also cover the generated file
    carrying the flag and the order the two commands are emitted in.
    """

    def test_an_output_value_survives_a_real_save_and_reload(self, engine: Engine, tmp_path: Path) -> None:
        flow_name, library_name = _fresh_flow(engine, "load_keeps_output", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "typed")
        _record_output_value(engine, node_name, "value", "produced")

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "load_keeps_output")
        _reload_from_disk(engine, file_path)

        assert _node(engine, node_name).parameter_output_values["value"] == "produced"

    def test_a_connected_target_keeps_its_output_value_through_a_reload(self, engine: Engine, tmp_path: Path) -> None:
        """Re-creating a connection during load must not push a value that discards the restored output."""
        flow_name, library_name = _fresh_flow(engine, "load_keeps_connected_output", tmp_path)
        source_name = _create_round_trip_node(engine, "Source", flow_name, library_name)
        target_name = _create_round_trip_node(engine, "Target", flow_name, library_name)
        _set_value(engine, source_name, "value", "from source")

        connect_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_name,
                source_parameter_name="value",
                target_node_name=target_name,
                target_parameter_name="value",
            )
        )
        assert isinstance(connect_result, CreateConnectionResultSuccess), connect_result

        # Written directly, as a run does: a connected parameter refuses a value set by request, and
        # doing it after the connection keeps the value that connecting pushed from discarding it.
        _node(engine, target_name).parameter_output_values["value"] = "produced"

        file_path = _save_flow_to_disk(engine, flow_name, tmp_path, "load_keeps_connected_output")
        _reload_from_disk(engine, file_path)

        assert _node(engine, target_name).parameter_output_values["value"] == "produced"


class TestDiscardedOutputValueEvent:
    def test_event_carries_the_set_value_not_none(self, engine: Engine, tmp_path: Path) -> None:
        """Consumers display whatever the event carries, so emitting None would blank the field."""
        _flow_name, _library_name, node_name = _ran_once(engine, tmp_path, "event_payload")
        node = _node(engine, node_name)
        emitted: list[tuple[str, Any]] = []

        def record(parameter_name: str, value: Any, *, deleted: bool = False) -> None:  # noqa: ARG001
            emitted.append((parameter_name, value))

        node.parameter_output_values._emit_parameter_change_event = record

        del node.parameter_output_values["value"]

        assert emitted == [("value", "original")]


class TestValuesTheGuardCannotCompareOrResolve:
    """Values a node can legitimately hold that a naive read or comparison chokes on."""

    def test_an_edit_discards_an_output_value_held_in_another_process(self, engine: Engine, tmp_path: Path) -> None:
        """The arriving value is read raw: resolving a reference held elsewhere raises, failing the set.

        This is the shape the orchestrator produces when it pushes a worker's output into the next node:
        the value that lands is an envelope naming an object another process holds.
        """
        flow_name, library_name = _fresh_flow(engine, "held_elsewhere", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "original")
        _record_output_value(engine, node_name, "value", "produced")
        elsewhere = make_reference(worker="another-worker", key="some-key", source="Producer.value")

        _set_value(engine, node_name, "value", elsewhere)

        assert "value" not in _node(engine, node_name).parameter_output_values

    def test_an_edit_discards_an_output_value_that_does_not_compare_as_a_bool(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """An array-like ``__ne__`` returns an array, so the recompute check must not ask for its truth.

        Asserted against the check itself rather than through a set request, because a set carrying an
        array-like output value cannot complete today: the side-effect propagation that runs after this
        check compares the same pair with a bare ``==`` in a boolean context and raises. That is
        pre-existing and left alone, so driving this from a request would fail either way and prove
        nothing about the check.
        """
        flow_name, library_name = _fresh_flow(engine, "array_like_output", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        node = _node(engine, node_name)
        snapshot = {"value": _ArrayLike([1, 2, 3])}
        node.parameter_output_values["value"] = _ArrayLike([4, 5, 6])

        engine.node_manager._discard_stale_output_value(node, "value", snapshot)

        assert node.parameter_output_values["value"].items == [4, 5, 6]

    def test_an_output_value_first_written_during_the_set_is_kept_even_when_it_is_none(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Nothing was recorded before the set, so whatever is there now was written during it."""
        flow_name, library_name = _fresh_flow(engine, "first_write_none", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        _set_value(engine, node_name, "value", "original")
        node = _node(engine, node_name)

        def write_none(parameter: Parameter, value: Any) -> None:  # noqa: ARG001
            node.parameter_output_values["value"] = None

        node.after_value_set = write_none

        _set_value(engine, node_name, "value", "edited")

        assert "value" in node.parameter_output_values


class TestRenameReportsBothNames:
    def test_renaming_a_parameter_reports_the_old_name_and_the_new_one(self, engine: Engine, tmp_path: Path) -> None:
        """Before, the move happened after the rename, so nothing was reported for the old name."""
        flow_name, library_name = _fresh_flow(engine, "rename", tmp_path)
        node_name = _create_round_trip_node(engine, "Holder", flow_name, library_name)
        node = _node(engine, node_name)
        node.add_parameter(
            Parameter(
                name="custom",
                type="any",
                default_value=None,
                tooltip="",
                user_defined=True,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        _set_value(engine, node_name, "custom", "typed")
        _record_output_value(engine, node_name, "custom", "produced")
        emitted: list[tuple[str, Any, bool]] = []

        def record(parameter_name: str, value: Any, *, deleted: bool = False) -> None:
            emitted.append((parameter_name, value, deleted))

        node.parameter_output_values._emit_parameter_change_event = record

        result = engine.handle_request(
            RenameParameterRequest(parameter_name="custom", new_parameter_name="renamed", node_name=node_name)
        )
        assert isinstance(result, RenameParameterResultSuccess), result

        assert emitted == [("custom", "typed", True), ("renamed", "produced", False)]
        assert node.parameter_output_values == {"renamed": "produced"}
        assert node.parameter_values["renamed"] == "typed"
