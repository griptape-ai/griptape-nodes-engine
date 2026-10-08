"""Setting a whole list on a ParameterList through SetParameterValueRequest.

The editor fills a list one row at a time, and the node reads its list back from those rows.
A request that hands the list over in one piece (an MCP client, an agent, a script) has to land
in the same place, or the request succeeds and the node runs with an empty list.
"""

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
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


def _rows(node: _ListNode) -> list[Parameter]:
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

    def test_clearing_a_resolved_node_unresolves_it(self, engine: Engine, flow_name: str) -> None:
        """No row is set when the list is cleared, so the row path cannot be what unresolves the node."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a"])
        node.state = NodeResolutionState.RESOLVED

        _set_items(engine, [])

        assert node.state == NodeResolutionState.UNRESOLVED

    def test_a_failed_item_still_unresolves_the_node(self, engine: Engine, flow_name: str) -> None:
        """The old rows are gone by the time an item fails, so the node no longer holds what it ran with."""
        node = _add(engine, _ListNode(name="Lister"), flow_name)
        _set_items(engine, ["a", "b"])
        node.state = NodeResolutionState.RESOLVED

        result = _set_items(engine, ["bad"])

        assert isinstance(result, SetParameterValueResultFailure)
        assert node.state == NodeResolutionState.UNRESOLVED

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
