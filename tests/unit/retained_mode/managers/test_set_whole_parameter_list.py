"""Setting a whole list on a ParameterList through SetParameterValueRequest.

The editor fills a list one row at a time, and the node reads its list back from those rows.
A request that hands the list over in one piece (an MCP client, an agent, a script) has to land
in the same place, or the request succeeds and the node runs with an empty list.
"""

from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.list_rows import ListRows
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState, aprocess_scope
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    RemoveParameterFromNodeRequest,
    RemoveParameterFromNodeResultSuccess,
    ReorderParameterListItemRequest,
    ReorderParameterListItemResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)


def _reject_bad(_parameter: Parameter, value: object) -> None:
    if value == "bad":
        msg = "bad item"
        raise ValueError(msg)


class _ListNode(BaseNode):
    def __init__(self, name: str, max_items: int | None = None) -> None:
        super().__init__(name=name)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                max_items=max_items,
                validators=[_reject_bad],
            )
        )

    def process(self) -> None:
        return None


class _HookedListNode(_ListNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.seen: list[object] = []

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.name != "items":
            self.seen.append(value)


class _Source(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.add_parameter(
            Parameter(name="out", output_type="list[str]", tooltip="", allowed_modes={ParameterMode.OUTPUT})
        )
        self.add_parameter(Parameter(name="text", output_type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        return None


@pytest.fixture
def flow_name(engine: Engine) -> str:
    """A flow to hang the nodes off, inside an active workflow."""
    engine.context_manager.push_workflow("wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None))
    assert isinstance(result, CreateFlowResultSuccess)
    return result.flow_name


def _add[NodeT: BaseNode](engine: Engine, node: NodeT, flow_name: str) -> NodeT:
    """Register a hand-built node the way CreateNodeRequest does, with no library to create it from."""
    engine.flow_manager.get_flow_by_name(flow_name).add_node(node)
    engine.object_manager.add_object_by_name(node.name, node)
    engine.node_manager._name_to_parent_flow_name[node.name] = flow_name
    return node


def _set_items(engine: Engine, value: object) -> SetParameterValueResultSuccess | SetParameterValueResultFailure:
    result = engine.handle_request(SetParameterValueRequest(node_name="Lister", parameter_name="items", value=value))
    assert isinstance(result, SetParameterValueResultSuccess | SetParameterValueResultFailure)
    return result


def _rows(node: BaseNode) -> list[Parameter]:
    items = node.get_parameter_by_name("items")
    assert isinstance(items, ParameterList)
    return items.get_child_parameters()


class TestWholeListSetOnAnUnconnectedList:
    def test_the_node_sees_the_list(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        result = _set_items(engine, ["a", "b", "c"])

        assert isinstance(result, SetParameterValueResultSuccess)
        assert node.get_parameter_value("items") == ["a", "b", "c"]

    def test_each_item_becomes_a_row(self, engine: Engine, flow_name: str) -> None:
        """Rows are what the editor shows and what a saved workflow keeps."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b", "c"])

        assert [node.get_parameter_value(row.name) for row in _rows(node)] == ["a", "b", "c"]

    def test_it_replaces_the_existing_rows(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["old1", "old2", "old3"])

        _set_items(engine, ["new"])

        assert node.get_parameter_value("items") == ["new"]
        assert len(_rows(node)) == 1

    def test_replaced_rows_leave_no_values_behind(self, engine: Engine, flow_name: str) -> None:
        """A value kept for a removed row is sent to execution, which has no such row and warns."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a", "b", "c", "d"])

        _set_items(engine, ["x"])

        row_value_names = {name for name in node.parameter_values if name.startswith("items_")}
        assert row_value_names == {row.name for row in _rows(node)}

    def test_an_empty_list_clears_the_rows(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a", "b"])

        _set_items(engine, [])

        assert node.get_parameter_value("items") == []
        assert _rows(node) == []
        get_result = engine.handle_request(GetParameterValueRequest(node_name="Lister", parameter_name="items"))
        assert isinstance(get_result, GetParameterValueResultSuccess)
        assert get_result.value == []

    def test_more_items_than_max_items_is_rejected_and_keeps_the_rows(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister", max_items=2), flow_name)
        _set_items(engine, ["a"])

        result = _set_items(engine, ["x", "y", "z"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == ["a"]

    def test_a_nested_list_adds_its_items(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        result = _set_items(engine, [["a", "b"], "c"])

        assert isinstance(result, SetParameterValueResultSuccess)
        assert node.get_parameter_value("items") == ["a", "b", "c"]
        assert [node.get_parameter_value(row.name) for row in _rows(node)] == [["a", "b"], "c"]

    def test_more_items_than_max_items_after_nested_lists_is_rejected_and_keeps_the_rows(
        self, engine: Engine, flow_name: str
    ) -> None:
        node = _add(engine, _ListNode(name="Lister", max_items=2), flow_name)
        _set_items(engine, ["a"])

        result = _set_items(engine, [["x", "y"], "z"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == ["a"]

    def test_clearing_a_resolved_node_unresolves_it(self, engine: Engine, flow_name: str) -> None:
        """No row is set when the list is cleared, so the row path cannot be what unresolves the node."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a"])
        node.state = NodeResolutionState.RESOLVED

        _set_items(engine, [])

        assert node.state == NodeResolutionState.UNRESOLVED

    def test_a_failed_item_puts_the_old_items_back(self, engine: Engine, flow_name: str) -> None:
        """A failure partway through must not leave the list half replaced."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a", "b"])

        result = _set_items(engine, ["ok", "bad", "never reached"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == ["a", "b"]
        assert [node.get_parameter_value(row.name) for row in _rows(node)] == ["a", "b"]

    def test_a_failed_item_on_an_empty_list_leaves_it_empty(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        result = _set_items(engine, ["ok", "bad"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == []
        assert _rows(node) == []

    def test_a_connected_row_is_rejected_and_keeps_its_connection(self, engine: Engine, flow_name: str) -> None:
        """Replacing the rows would delete the row's connection with no word to the caller."""
        _add(engine, _Source(name="Source"), flow_name)
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a"])
        row = _rows(node)[0]
        connect = engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Lister",
                target_parameter_name=row.name,
            )
        )
        assert isinstance(connect, CreateConnectionResultSuccess)

        result = _set_items(engine, ["x", "y"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert _rows(node) == [row]


class TestValuesThatAreNotLists:
    def test_a_string_is_rejected(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a"])

        result = _set_items(engine, "abc")

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == ["a"]

    def test_none_is_not_rejected(self, engine: Engine, flow_name: str) -> None:
        """None is how a value gets cleared, so it keeps going through the normal path."""
        _add(engine, _ListNode(name="Lister"), flow_name)

        result = _set_items(engine, None)

        assert isinstance(result, SetParameterValueResultSuccess)


class TestWhoMayReplaceTheRows:
    def test_a_node_that_is_not_settable_is_rejected(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a"])
        items = node.get_parameter_by_name("items")
        assert items is not None
        items.settable = False

        result = _set_items(engine, ["x"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.get_parameter_value("items") == ["a"]

    def test_the_node_hears_about_each_row(self, engine: Engine, flow_name: str) -> None:
        """Hooks on row sets are how libraries react to a list changing, so they must fire."""
        node = _add(engine, _HookedListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert node.seen == ["a", "b"]


class TestWholeListArrivingOverAConnection:
    def test_it_does_not_become_rows(self, engine: Engine, flow_name: str) -> None:
        """A connected list is read whole, so rows would only be shadowed copies of it."""
        _add(engine, _Source(name="Source"), flow_name)
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        connect = engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="out",
                target_node_name="Lister",
                target_parameter_name="items",
            )
        )
        assert isinstance(connect, CreateConnectionResultSuccess)

        result = engine.handle_request(
            SetParameterValueRequest(
                node_name="Lister",
                parameter_name="items",
                value=["a", "b"],
                incoming_connection_source_node_name="Source",
                incoming_connection_source_parameter_name="out",
            )
        )

        assert isinstance(result, SetParameterValueResultSuccess)
        assert node.get_parameter_value("items") == ["a", "b"]
        assert _rows(node) == []


class _OutputListNode(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.add_parameter(ParameterList(name="items", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        return None


def _get_items(engine: Engine) -> object:
    result = engine.handle_request(GetParameterValueRequest(node_name="Lister", parameter_name="items"))
    assert isinstance(result, GetParameterValueResultSuccess)
    return result.value


def _add_row(engine: Engine, value: str) -> str:
    added = engine.handle_request(AddParameterToNodeRequest(node_name="Lister", parent_container_name="items"))
    assert isinstance(added, AddParameterToNodeResultSuccess)
    engine.handle_request(
        SetParameterValueRequest(node_name="Lister", parameter_name=added.parameter_name, value=value)
    )
    return added.parameter_name


class TestTheListFollowsItsRows:
    """The list's own entry is what execution, saving and GetParameterValueRequest read."""

    def test_removing_a_row_takes_its_item_out(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        rows = [_add_row(engine, value) for value in ("a", "b", "c")]

        removed = engine.handle_request(RemoveParameterFromNodeRequest(node_name="Lister", parameter_name=rows[1]))

        assert isinstance(removed, RemoveParameterFromNodeResultSuccess)
        assert _get_items(engine) == ["a", "c"]
        assert rows[1] not in node.parameter_values

    def test_moving_a_row_moves_its_item(self, engine: Engine, flow_name: str) -> None:
        _add(engine, _ListNode(name="Lister"), flow_name)
        for value in ("a", "b", "c"):
            _add_row(engine, value)

        moved = engine.handle_request(
            ReorderParameterListItemRequest(node_name="Lister", parameter_list_name="items", from_index=0, to_index=2)
        )

        assert isinstance(moved, ReorderParameterListItemResultSuccess)
        assert _get_items(engine) == ["b", "c", "a"]

    def test_an_output_only_list_is_left_to_its_node(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _OutputListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert _rows(node) == []

    def test_a_worker_is_sent_the_list_its_node_reads(self, engine: Engine, flow_name: str) -> None:
        """A workflow saved before the list followed its rows can hold items whose rows are gone."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        node.parameter_values["items"] = ["removed"]

        assert node._list_rows.values_for_dispatch()["items"] == []


class TestLoadingASavedWorkflow:
    def test_saved_rows_win_over_the_saved_list(self, engine: Engine, flow_name: str) -> None:
        """A saved file sets the list first, then each row, so the list must not become rows."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        row = _add_row(engine, "")

        for name, value in (("items", ["saved"]), (row, "saved")):
            engine.handle_request(
                SetParameterValueRequest(node_name="Lister", parameter_name=name, value=value, initial_setup=True)
            )

        assert [r.name for r in _rows(node)] == [row]
        assert node.get_parameter_value("items") == ["saved"]

    def test_a_list_saved_without_rows_gets_no_rows(self, engine: Engine, flow_name: str) -> None:
        """Only saved rows become rows."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        engine.handle_request(
            SetParameterValueRequest(node_name="Lister", parameter_name="items", value=["a", "b"], initial_setup=True)
        )

        assert _rows(node) == []


def _shout(value: object) -> object:
    if isinstance(value, str):
        return value.upper()
    return value


class _ConvertingListNode(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                item_converters=[_shout],
            )
        )

    def process(self) -> None:
        return None


class TestItemConverters:
    def test_a_whole_list_set_converts_each_row(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ConvertingListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert [node.get_parameter_value(row.name) for row in _rows(node)] == ["A", "B"]
        assert _get_items(engine) == ["A", "B"]

    def test_a_row_set_by_the_editor_converts(self, engine: Engine, flow_name: str) -> None:
        _add(engine, _ConvertingListNode(name="Lister"), flow_name)

        _add_row(engine, "a")

        assert _get_items(engine) == ["A"]

    def test_a_row_restored_from_a_saved_workflow_converts(self, engine: Engine, flow_name: str) -> None:
        """A saved row is rebuilt from its saved fields, and converters are not saved."""
        node = _add(engine, _ConvertingListNode(name="Lister"), flow_name)
        row_name = "items_ParameterListUniqueParamID_00000000000000000000000000000001"
        engine.handle_request(
            AddParameterToNodeRequest(
                node_name="Lister",
                parameter_name=row_name,
                parent_container_name="items",
                tooltip="",
                input_types=["str"],
                is_user_defined=True,
                mode_allowed_property=True,
                initial_setup=True,
            )
        )

        engine.handle_request(SetParameterValueRequest(node_name="Lister", parameter_name=row_name, value="a"))

        assert node.get_parameter_value(row_name) == "A"

    def test_a_whole_list_over_a_connection_converts_each_item(self, engine: Engine, flow_name: str) -> None:
        _add(engine, _Source(name="Source"), flow_name)
        node = _add(engine, _ConvertingListNode(name="Lister"), flow_name)
        engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="out",
                target_node_name="Lister",
                target_parameter_name="items",
            )
        )

        engine.handle_request(
            SetParameterValueRequest(
                node_name="Lister",
                parameter_name="items",
                value=["a", "b"],
                incoming_connection_source_node_name="Source",
                incoming_connection_source_parameter_name="out",
            )
        )

        assert node.get_parameter_value("items") == ["A", "B"]
        assert _rows(node) == []


class _NormalizingListNode(_ListNode):
    """Rewrites its list in `after_value_set`, as libraries written for older engines do."""

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.name == "items" and isinstance(value, list):
            shouted = [_shout(item) for item in value]
            if shouted != value:
                self.set_parameter_value("items", shouted)


class TestNodeCodeWritingItsList:
    def test_it_fills_the_rows(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _add_row(engine, "a")

        node.set_parameter_value("items", ["x", "y"])

        assert [node.get_parameter_value(row.name) for row in _rows(node)] == ["x", "y"]
        assert "items" not in node.parameter_values

    def test_a_running_node_leaves_the_rows(self, engine: Engine, flow_name: str) -> None:
        """Rows a node adds while it runs in a worker would not reach the orchestrator."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        with aprocess_scope(node=node):
            node.set_parameter_value("items", ["a", "b"])

        assert _rows(node) == []

    def test_a_hook_rewriting_its_list_after_a_row_edit_changes_nothing(self, engine: Engine, flow_name: str) -> None:
        """A row edit from the editor fires the list's hook, and a rewrite there would replace every row."""
        node = _add(engine, _NormalizingListNode(name="Lister"), flow_name)
        row = _add_row(engine, "a")

        assert [r.name for r in _rows(node)] == [row]
        assert _get_items(engine) == ["a"]

    def test_clearing_its_value_leaves_the_rows(self, engine: Engine, flow_name: str) -> None:
        """Disconnecting a list handed a whole list clears its value this way."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _add_row(engine, "a")

        node.remove_parameter_value("items")

        assert _get_items(engine) == ["a"]

    def test_a_running_node_records_its_output_list(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _OutputListNode(name="Lister"), flow_name)

        with aprocess_scope(node=node):
            node.set_parameter_value("items", ["a", "b"])

        assert node.parameter_output_values["items"] == ["a", "b"]

    def test_a_hook_rewriting_the_list_while_rows_fill_changes_nothing(self, engine: Engine, flow_name: str) -> None:
        """Libraries written for older engines normalize their list this way."""
        node = _add(engine, _NormalizingListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert [node.get_parameter_value(row.name) for row in _rows(node)] == ["a", "b"]
        assert _get_items(engine) == ["a", "b"]


def _bracket(value: object) -> object:
    """Not idempotent, so running it twice on an item shows. Passes None through, as clearing a row sends it."""
    if value is None:
        return None
    return f"[{value}]"


class _BracketingListNode(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                item_converters=[_bracket],
            )
        )

    def process(self) -> None:
        return None


class TestItemConvertersRunOncePerItem:
    def test_a_row_set_by_the_editor(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _BracketingListNode(name="Lister"), flow_name)

        row = _add_row(engine, "a")

        assert node.get_parameter_value(row) == "[a]"
        assert _get_items(engine) == ["[a]"]

    def test_a_whole_list_set_by_request(self, engine: Engine, flow_name: str) -> None:
        _add(engine, _BracketingListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert _get_items(engine) == ["[a]", "[b]"]


class TestDispatch:
    def test_a_list_is_sent_without_its_rows(self, engine: Engine, flow_name: str) -> None:
        """A worker builds its own rows, with new names, from the list."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        rows = [_add_row(engine, value) for value in ("a", "b")]

        values = node._list_rows.values_for_dispatch()

        assert values["items"] == ["a", "b"]
        assert not set(rows) & set(values)

    def test_a_list_with_nested_lists_is_sent_concatenated(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, [["a", "b"], "c"])

        values = node._list_rows.values_for_dispatch()

        assert values["items"] == ["a", "b", "c"]

    def test_a_list_built_from_its_rows_is_sent(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        row = _add_row(engine, "a")
        assert "items" not in node.parameter_values

        values = node._list_rows.values_for_dispatch()

        assert values["items"] == ["a"]
        assert row not in values


class TestFillingRowsOnACopyWithNone:
    def test_it_does_not_ask_about_connections_to_rows_it_does_not_have(self, engine: Engine, flow_name: str) -> None:
        """On a worker that question goes to the orchestrator, so it costs a round trip."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        with patch.object(ListRows, "_connected_parameter_names", return_value=set()) as asked:
            _set_items(engine, ["a"])

        asked.assert_not_called()
        assert _get_items(engine) == ["a"]
        assert len(_rows(node)) == 1


class _ListHookNode(_BracketingListNode):
    """Records each value its list's `after_value_set` receives, as Path Join's output does."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.seen_lists: list[object] = []

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.name == "items":
            self.seen_lists.append(value)


class TestTheListHearsEveryRowEdit:
    def test_removing_a_row(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListHookNode(name="Lister"), flow_name)
        rows = [_add_row(engine, value) for value in ("a", "b")]
        node.seen_lists.clear()

        engine.handle_request(RemoveParameterFromNodeRequest(node_name="Lister", parameter_name=rows[0]))

        assert node.seen_lists == [["[b]"]]

    def test_moving_a_row(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListHookNode(name="Lister"), flow_name)
        for value in ("a", "b"):
            _add_row(engine, value)
        node.seen_lists.clear()

        engine.handle_request(
            ReorderParameterListItemRequest(node_name="Lister", parameter_list_name="items", from_index=0, to_index=1)
        )

        assert node.seen_lists == [["[b]", "[a]"]]

    def test_a_whole_list_set_is_heard_once(self, engine: Engine, flow_name: str) -> None:
        """Not once per row with the list half filled."""
        node = _add(engine, _ListHookNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert node.seen_lists == [["[a]", "[b]"]]

    def test_clearing_a_row_value_converts_the_rest_once(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListHookNode(name="Lister"), flow_name)
        rows = [_add_row(engine, value) for value in ("a", "b")]

        node.remove_parameter_value(rows[0])

        assert _get_items(engine) == ["[b]"]


class TestARowEditOnAConnectedList:
    def test_keeps_the_value_its_connection_delivered(self, engine: Engine, flow_name: str) -> None:
        _add(engine, _Source(name="Source"), flow_name)
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        row = _add_row(engine, "manual")
        engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="out",
                target_node_name="Lister",
                target_parameter_name="items",
            )
        )
        engine.handle_request(
            SetParameterValueRequest(
                node_name="Lister",
                parameter_name="items",
                value=["x", "y"],
                incoming_connection_source_node_name="Source",
                incoming_connection_source_parameter_name="out",
            )
        )

        engine.handle_request(RemoveParameterFromNodeRequest(node_name="Lister", parameter_name=row))

        assert node.get_parameter_value("items") == ["x", "y"]


class TestAnUnconnectedListStoresNothing:
    def test_after_rows_are_set_removed_and_moved(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        rows = [_add_row(engine, value) for value in ("a", "b", "c")]
        engine.handle_request(RemoveParameterFromNodeRequest(node_name="Lister", parameter_name=rows[0]))
        engine.handle_request(
            ReorderParameterListItemRequest(node_name="Lister", parameter_list_name="items", from_index=0, to_index=1)
        )

        assert "items" not in node.parameter_values
        assert _get_items(engine) == ["c", "b"]

    def test_after_a_whole_list_set(self, engine: Engine, flow_name: str) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        _set_items(engine, ["a", "b"])

        assert "items" not in node.parameter_values


class _RowHookRewritesList(_ListNode):
    """Rewrites its whole list from a row's hook, not the list's own."""

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.parent_container_name == "items" and isinstance(value, str) and not value.isupper():
            self.set_parameter_value("items", [str(item).upper() for item in self.get_parameter_value("items")])


class TestAHookWritingItsList:
    def test_from_a_row_hook_leaves_the_rows(self, engine: Engine, flow_name: str) -> None:
        """Replacing the rows there would remove the row being set."""
        node = _add(engine, _RowHookRewritesList(name="Lister"), flow_name)

        rows = [_add_row(engine, value) for value in ("a", "b")]

        assert [row.name for row in _rows(node)] == rows
        assert _get_items(engine) == ["a", "b"]

    def test_is_warned_about_once(self, engine: Engine, flow_name: str, caplog: pytest.LogCaptureFixture) -> None:
        _add(engine, _RowHookRewritesList(name="Lister"), flow_name)

        with caplog.at_level("WARNING", logger="griptape_nodes"):
            for value in ("a", "b"):
                _add_row(engine, value)

        warnings = [record for record in caplog.records if "from a value hook" in record.getMessage()]
        assert len(warnings) == 1

    def test_that_changes_nothing_is_not_warned_about(
        self, engine: Engine, flow_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Libraries rewrite their list only when converting changed it."""
        _add(engine, _NormalizingListNode(name="Lister"), flow_name)

        with caplog.at_level("WARNING", logger="griptape_nodes"):
            _add_row(engine, "A")

        assert "from a value hook" not in caplog.text


class TestAWorkerFillingItsCopy:
    @pytest.fixture
    def worker(self, engine: Engine) -> object:
        engine.library_manager._is_worker = True
        yield
        engine.library_manager._is_worker = False

    @pytest.mark.usefixtures("worker")
    @pytest.mark.parametrize("items", [["a", "b", "c"], []])
    def test_does_not_ask_the_orchestrator_about_connections(
        self, engine: Engine, flow_name: str, items: list[str]
    ) -> None:
        node = _add(engine, _ListNode(name="Lister"), flow_name)

        with patch.object(BaseNode, "_param_has_incoming_connection", return_value=False) as asked:
            failure = engine.node_manager._apply_hydrated_values(node, "Lister", {"items": items})

        assert failure is None
        asked.assert_not_called()
        assert node.get_parameter_value("items") == items

    @pytest.mark.usefixtures("worker")
    def test_a_connected_list_over_max_items_runs_with_every_item(self, engine: Engine, flow_name: str) -> None:
        """The orchestrator warns and runs with the whole list, so the worker must too."""
        node = _add(engine, _ListNode(name="Lister", max_items=2), flow_name)

        with patch.object(BaseNode, "_param_has_incoming_connection", return_value=True):
            failure = engine.node_manager._apply_hydrated_values(node, "Lister", {"items": ["a", "b", "c"]})
            value = node.get_parameter_value("items")

        assert failure is None
        assert value == ["a", "b", "c"]
        assert _rows(node) == []
