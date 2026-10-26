"""A node's list parameters and their rows.

A list nothing is connected to holds its items in rows, child parameters that each hold one item or a list
of items, and has no value of its own: reading it builds it from the rows. Only a list fed by a connection to the list itself stores the
whole list it was handed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeGuard

from griptape_nodes.exe_types.core_types import Parameter, ParameterContainer, ParameterList, ParameterMode
from griptape_nodes.exe_types.parameter_mutation import sanctioned_parameter_mutation
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.core_types import BaseNodeElement
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


class ListRows:
    """The rows of one node's lists: replacing them, and telling the node when they change."""

    def __init__(self, node: BaseNode) -> None:
        self._node = node
        # Lists whose rows `_replace` is replacing. The node hears about each once, when every row is in place.
        self._being_replaced: set[str] = set()
        # Lists whose value hooks, or one of whose rows' value hooks, are running.
        self._in_hooks: set[str] = set()
        # Lists a dropped hook write has already been warned about.
        self._warned: set[str] = set()

    def holds_no_value(self, parameter: Parameter) -> TypeGuard[ParameterList]:
        """Whether `parameter` is a list read from its rows, which keeps no value of its own."""
        if not isinstance(parameter, ParameterList):
            return False
        if ParameterMode.INPUT not in parameter.allowed_modes:
            return True
        return not self._node._param_has_incoming_connection(parameter.name)

    @contextmanager
    def hooks_running(self, list_name: str | None) -> Iterator[None]:
        """Mark `list_name` while a value hook on it or one of its rows runs. A no-op for None."""
        if list_name is None or list_name in self._in_hooks:
            yield
            return
        self._in_hooks.add(list_name)
        try:
            yield
        finally:
            self._in_hooks.discard(list_name)

    def write_from_node(self, parameter: Parameter, value: Any, *, initial_setup: bool, running: bool) -> bool:
        """Handle the node's own write to a list that keeps no value. Returns False for any other parameter.

        A whole list becomes the rows, as it does from a request, except where that would undo the rows:

        - while the node is running, since rows it adds then don't reach the orchestrator's copy. Its
          result is recorded instead.
        - while loading, which sets the rows next.
        - from a value hook on the list or one of its rows, which runs while a row is being set.
          Replacing the rows there would remove the one being set. To convert items, a list uses
          `item_converters` instead.
        """
        if not self.holds_no_value(parameter):
            return False
        name = parameter.name
        self._node.parameter_values.pop(name, None)
        if running:
            self._node._also_record_as_result(parameter, value)
            return True
        if name in self._in_hooks:
            self._warn_hook_write(parameter, value)
            return True
        if initial_setup or name in self._being_replaced:
            return True
        self.set_as_rows_if_unconnected(parameter, value)
        return True

    def fill_copy(self, parameter: Parameter, value: Any) -> bool:
        """Build the rows of a worker's copy of the node from the list it was sent. Returns whether it did.

        Unlike `set_as_rows_if_unconnected`, this doesn't ask the orchestrator whether the list is
        connected: a fresh copy has no rows to protect, and rows built from the list give the node the
        same items either way.

        A list over `max_items` can only have come over a connection, and doesn't fit in rows. It is
        stored whole instead, so the node runs with every item as it does on the orchestrator.
        """
        if not isinstance(parameter, ParameterList) or not isinstance(value, list):
            return False
        if not _accepts_whole_list(parameter):
            return False
        max_items = parameter.max_items
        if max_items is not None and _item_count(parameter, value) > max_items:
            return False
        if value != handle_container_parameter(self._node, parameter):
            self._replace(parameter, value)
        return True

    def set_as_rows_if_unconnected(self, parameter: Parameter, value: Any) -> bool:
        """Write a whole list to the rows of an unconnected list. Returns whether it did.

        The engine calls this where a value arrives from outside the node: a request, and a worker
        copy receiving the list it runs with. An unconnected list is read from its rows, so a list
        stored beside them would never be read.

        Raises:
            TypeError: if `value` is not a list.
            ValueError: if there are more items than `max_items`, or a row is connected to another node.
        """
        if not isinstance(parameter, ParameterList) or value is None:
            # None is how a value is cleared, and keeps its old meaning.
            return False
        if not _accepts_whole_list(parameter):
            return False
        if isinstance(value, list) and value == handle_container_parameter(self._node, parameter):
            # Already what the rows hold.
            return False
        if self._node._param_has_incoming_connection(parameter.name):
            # A connected list is read whole, and keeps the value as it arrived.
            return False
        self._replace(parameter, value)
        return True

    def convert_and_validate(self, parameter: Parameter, value: Any) -> Any:
        """Run `parameter`'s converters and validators on `value`, or on each item of a list a row adds."""
        convert_and_validate = self._node._convert_and_validate
        container = self._container_of(parameter)
        if not isinstance(container, ParameterList) or not container.row_value_is_items(value):
            return convert_and_validate(parameter, value)
        return [convert_and_validate(parameter, item) for item in value]

    def check_row_within_max_items(self, row: Parameter, value: Any, *, initial_setup: bool) -> None:
        if initial_setup:
            return
        node = self._node
        container = self._container_of(row)
        if not isinstance(container, ParameterList) or container.max_items is None:
            return
        held_count = 0
        for child in container.get_child_parameters():
            child_value = value if child.name == row.name else node._get_raw_parameter_value(child.name)
            held_count += len(_items_of_row(container, child_value))
        if held_count <= container.max_items:
            return
        # Workers rebuild one row per item, so values over `max_items` cannot be restored.
        msg = f"Attempted to set an item of list '{node.name}.{container.name}'. Failed because the list would hold {held_count} items, counting the items of any list in a row, and holds at most {container.max_items} items."
        raise ValueError(msg)

    def row_removed(self, parameter_list: ParameterList, row: BaseNodeElement) -> None:
        """Drop a removed row's value, and tell the node its list changed."""
        self._node.parameter_values.pop(row.name, None)
        self.rows_changed(parameter_list)

    def rows_changed(self, container: ParameterContainer, *, initial_setup: bool = False) -> None:
        """Tell the node a container's rows changed: a row was set, removed or moved."""
        if container.name in self._being_replaced:
            return
        if not self.holds_no_value(container):
            # A connected list holds the value its connection delivered, which the rows don't change.
            return
        self._notify(container, initial_setup=initial_setup)

    def values_for_dispatch(self) -> dict[str, Any]:
        """`parameter_values`, with each list as the node reads it and without its rows, to send with `ExecuteNodeRequest`.

        A worker copy has no rows and builds its own, with new names, from the list.
        """
        node = self._node
        values = dict(node.parameter_values)
        for parameter in node.parameters:
            if not isinstance(parameter, ParameterList):
                continue
            rows = parameter.get_child_parameters()
            if parameter.name not in values and not rows:
                continue
            values[parameter.name] = node._get_raw_parameter_value(parameter.name)
            for row in rows:
                values.pop(row.name, None)
        return values

    def has_connected_rows(self, parameter: Parameter) -> bool:
        """Whether `parameter` is a list with a row connected to another node, so its rows can't be replaced.

        Checked only when there are rows: on a worker the question goes to the orchestrator.
        """
        if not isinstance(parameter, ParameterList):
            return False
        rows = parameter.get_child_parameters()
        if not rows:
            return False
        return bool(self._connected_parameter_names({row.name for row in rows}))

    def _replace(self, parameter_list: ParameterList, items: Any) -> None:
        """Replace the list's rows with one row per entry of `items`, the way the editor fills a list.

        An entry that is itself a list of items fills one row, and counts as its items toward `max_items`.

        Each row is set through `set_parameter_value`, so the node's hooks fire for each one. If an item
        is rejected, the old rows are put back and the error is raised.

        Raises:
            TypeError: if `items` is not a list.
            ValueError: if there are more items than `max_items`, or a row is connected to another node.
        """
        node = self._node
        list_label = f"'{node.name}.{parameter_list.name}'"
        if not isinstance(items, list):
            msg = f"Attempted to set list {list_label}. Failed because a list can only be set to a list, but got a value of type '{type(items).__name__}'."
            raise TypeError(msg)
        max_items = parameter_list.max_items
        item_count = _item_count(parameter_list, items)
        if max_items is not None and item_count > max_items:
            msg = f"Attempted to set list {list_label} to {item_count} items, counting the items of any list in it. Failed because the list holds at most {max_items} items."
            raise ValueError(msg)
        old_rows = parameter_list.get_child_parameters()
        if self.has_connected_rows(parameter_list):
            msg = f"Attempted to set list {list_label}. Failed because some of its items are connected to other nodes, and replacing the items would remove those connections. Disconnect them first."
            raise ValueError(msg)

        old_values = {
            row.name: node.parameter_values[row.name] for row in old_rows if row.name in node.parameter_values
        }
        self._being_replaced.add(parameter_list.name)
        try:
            with sanctioned_parameter_mutation():
                for row in old_rows:
                    node.remove_parameter_element(row)
            for item in items:
                row = parameter_list.add_child_parameter()
                node.set_parameter_value(row.name, item)
            self._notify(parameter_list, initial_setup=False)
        except Exception:
            # Converters, validators and hooks may raise anything. Undo, then let it propagate unchanged.
            self._restore(parameter_list, old_rows, old_values)
            raise
        finally:
            self._being_replaced.discard(parameter_list.name)

    def _container_of(self, parameter: Parameter) -> BaseNodeElement | None:
        if parameter.parent_container_name is None:
            return None
        return self._node.get_parameter_by_name(parameter.parent_container_name)

    def _restore(self, parameter_list: ParameterList, rows: list[Parameter], values: dict[str, Any]) -> None:
        """Put back the rows `_replace` removed, with their values, and remove the ones it added."""
        node = self._node
        with sanctioned_parameter_mutation():
            for row in parameter_list.get_child_parameters():
                node.remove_parameter_element(row)
        for row in rows:
            parameter_list.add_child(row)
        # Restored without running hooks, which could raise again and hide the error being undone.
        node.parameter_values.update(values)

    def _notify(self, container: ParameterContainer, *, initial_setup: bool) -> None:
        """Run the container's validators and hooks on the value its rows now build.

        Its converters don't run: each row ran them on its own item. The value isn't stored, since
        reading the list builds it again. A running node records it as its result.
        """
        node = self._node
        value = handle_container_parameter(node, container)
        for validator in container.validators:
            validator(container, value)
        node._also_record_as_result(container, value)
        if initial_setup:
            return
        with self.hooks_running(container.name):
            node.before_value_set(parameter=container, value=value)
            node.after_value_set(parameter=container, value=value)

    def _warn_hook_write(self, parameter: ParameterList, value: Any) -> None:
        """Warn, once per list, that a hook's write to it was ignored. Silent when it wouldn't change anything."""
        name = parameter.name
        if name in self._warned or value == handle_container_parameter(self._node, parameter):
            return
        self._warned.add(name)
        logger.warning(
            "Node '%s' set list '%s' from a value hook, which can't change a list's items, so the write was "
            "ignored. To convert each item, give the list item_converters.",
            self._node.name,
            name,
        )

    def _connected_parameter_names(self, parameter_names: set[str]) -> set[str]:
        """Which of `parameter_names` have a connection in either direction."""
        node = self._node
        result = node.engine.handle_request(ListConnectionsForNodeRequest(node_name=node.name, broadcast_result=False))
        if not isinstance(result, ListConnectionsForNodeResultSuccess):
            return set()
        connected = {connection.target_parameter_name for connection in result.incoming_connections}
        connected.update(connection.source_parameter_name for connection in result.outgoing_connections)
        return connected & parameter_names


def _accepts_whole_list(parameter_list: ParameterList) -> bool:
    """Whether a whole list can be written to the list's rows. An output-only list is built by its own node, row by row."""
    modes = parameter_list.allowed_modes
    return ParameterMode.INPUT in modes or ParameterMode.PROPERTY in modes


def handle_container_parameter(current_node: BaseNode, parameter: ParameterContainer) -> list[Any]:
    """Build a container's value from its rows."""
    build_parameter_value = []
    for child in parameter.find_elements_by_type(Parameter, find_recursively=False):
        # Raw, because what this builds is cached into the container's own entry in
        # `parameter_values`, and that dict is the payload of an ExecuteNodeRequest. Translating here
        # would put a live object in it and send it to a worker as JSON. The node still sees objects:
        # its read resolves the whole list on the way out.
        value = current_node._get_raw_parameter_value(child.name)
        build_parameter_value.extend(_items_of_row(parameter, value))
    return build_parameter_value


def _item_count(parameter_list: ParameterList, entries: list[Any]) -> int:
    """How many items the list holds with one row per entry."""
    return sum(len(_items_of_row(parameter_list, entry)) for entry in entries)


def _items_of_row(container: ParameterContainer, value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(container, ParameterList) and container.row_value_is_items(value):
        return [item for item in value if item is not None]
    return [value]
