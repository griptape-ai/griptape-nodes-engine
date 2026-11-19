"""A node's list parameters and their rows.

A list nothing is connected to holds its items in rows, child parameters that each hold one item or a list
of items, and has no value of its own: reading it builds it from the rows. Only a list fed by a connection to the list itself stores the
whole list it was handed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeGuard

from griptape_nodes.exe_types.core_types import Parameter, ParameterContainer, ParameterList, ParameterMode
from griptape_nodes.exe_types.parameter_mutation import sanctioned_parameter_mutation
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

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

    def list_of(self, parameter: Parameter) -> ParameterList | None:
        """The list `parameter` is, or the list it is a row of; None for any other parameter."""
        if isinstance(parameter, ParameterList):
            return parameter
        container = self._container_of(parameter)
        if not isinstance(container, ParameterList):
            return None
        return container

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
        - from a value hook on the list or one of its rows, which runs while a row is being set.
          Replacing the rows there would remove the one being set. To convert items, a list uses
          `item_converters` instead.

        While loading, a whole list is stored as any value is, without asking whether the list is
        connected: the connection may not be made yet. A save writes a whole list only for a list a
        connection fed, and the rows of any other list arrive as their own values.
        """
        if initial_setup:
            return False
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
        if name in self._being_replaced:
            return True
        self.set_as_rows_if_unconnected(parameter, value)
        return True

    def set_as_rows_if_unconnected(self, parameter: Parameter, value: Any) -> bool:
        """Write a whole list to the rows of an unconnected list. Returns whether it did.

        The engine calls this where a value arrives from outside the node: a request. An unconnected
        list is read from its rows, so a list stored beside them would never be read.

        Raises:
            TypeError: if `value` is not a list.
            ValueError: if there are more items than `max_items`, or a row is connected to another node.
        """
        if not isinstance(parameter, ParameterList) or value is None:
            # None is how a value is cleared, and keeps its old meaning.
            return False
        if not _accepts_whole_list(parameter):
            return False
        if self._node._param_has_incoming_connection(parameter.name):
            # A connected list keeps the value as it arrived, ahead of its rows.
            return False
        if isinstance(value, list) and value == handle_container_parameter(self._node, parameter):
            # Already what the rows hold.
            return False
        self._replace(parameter, value)
        return True

    def convert_and_validate(self, parameter: Parameter, value: Any, *, initial_setup: bool) -> Any:
        """Run `parameter`'s converters and validators on `value`, or on each item of a list a row adds.

        A connected list's validators check the list the node will read: `value`, then its rows' items.
        Not while loading, which doesn't ask whether the list is connected: the load may not have made
        the connection yet, or its rows. The value is checked as it is.
        """
        node = self._node
        if isinstance(parameter, ParameterList):
            candidate_value = node._convert_value(parameter, value)
            read_value = candidate_value if initial_setup else self.read_with_rows(parameter, candidate_value)
            for validator in parameter.validators:
                validator(parameter, read_value)
            return candidate_value
        convert_and_validate = node._convert_and_validate
        container = self._container_of(parameter)
        if not isinstance(container, ParameterList) or not container.row_value_is_items(value):
            return convert_and_validate(parameter, value)
        return [convert_and_validate(parameter, item) for item in value]

    def read_with_rows(self, parameter: Parameter, value: Any) -> Any:
        """What the node reads once a connected list stores `value`: those items, then its rows' items.

        Any other value, or a list read from its rows alone, is returned as it is.
        """
        if not isinstance(parameter, ParameterList) or not isinstance(value, list):
            return value
        if self.holds_no_value(parameter):
            return value
        return self.connected_then_rows(parameter, value)

    def connected_then_rows(self, parameter_list: ParameterList, connected: list[Any]) -> list[Any]:
        """The items a connection delivered, then the items of the list's own rows."""
        return [*connected, *handle_container_parameter(self._node, parameter_list)]

    def check_row_within_max_items(self, row: Parameter, value: Any, *, initial_setup: bool) -> None:
        if initial_setup:
            return
        reason = self._row_overrun_reason(row, value)
        if reason is not None:
            self._raise_overrun(reason)

    def check_connected_list_within_max_items(self, parameter: Parameter, value: Any, *, initial_setup: bool) -> None:
        """Refuse a list a connection delivers when it and the rows' items together exceed `max_items`."""
        if initial_setup or self.holds_no_value(parameter):
            return
        reason = self._list_overrun_reason(parameter, value)
        if reason is not None:
            self._raise_overrun(reason)

    def incoming_overrun_reason(self, target: Parameter, value: Any) -> str | None:
        """Why a connection to `target` delivering `value` would overrun `max_items`, or None if it fits.

        For a connection not made yet, so a list `target` counts `value` in place of any list it holds now.
        """
        if isinstance(target, ParameterList):
            return self._list_overrun_reason(target, value)
        return self._row_overrun_reason(target, value)

    def full_reason(self, parameter_list: ParameterList) -> str | None:
        """Why `parameter_list` takes no more items, or None while it has room. An empty item counts nothing."""
        max_items = parameter_list.max_items
        if max_items is None or self.offered_item_count(parameter_list, {}) < max_items:
            return None
        return self._full_message(parameter_list, max_items)

    def offered_item_count(self, parameter_list: ParameterList, offered: Mapping[str, Any]) -> int:
        """How many items the list would hold if every connection into it delivered what its source offers now.

        `offered` maps the list's name, or a row's name, to the value its source offers. A list or row
        missing from it counts what it holds now.
        """
        if parameter_list.name in offered:
            held_count = _connected_list_count(parameter_list, offered[parameter_list.name])
        else:
            held_count = self._connected_item_count(parameter_list)
        for row in parameter_list.get_child_parameters():
            if row.name in offered:
                row_value = offered[row.name]
            else:
                row_value = self._node._get_raw_parameter_value(row.name)
            held_count += _counted_items_of_entry(parameter_list, row_value)
        return held_count

    def _row_overrun_reason(self, row: Parameter, value: Any) -> str | None:
        container = self._container_of(row)
        if not isinstance(container, ParameterList) or container.max_items is None:
            return None
        return self._overrun_reason(container, row, value)

    def _list_overrun_reason(self, parameter: Parameter, value: Any) -> str | None:
        if not isinstance(parameter, ParameterList) or parameter.max_items is None:
            return None
        return self._overrun_reason(parameter, parameter, value)

    def _connected_item_count(self, parameter_list: ParameterList) -> int:
        if self.holds_no_value(parameter_list):
            return 0
        return _connected_list_count(parameter_list, self._node.parameter_values.get(parameter_list.name))

    def _overrun_reason(self, container: ParameterList, target: Parameter, value: Any) -> str | None:
        """Why `container` can't take `value` into `target`, its own value or one of its rows, or None if it fits."""
        max_items = container.max_items
        held_count = self.offered_item_count(container, {target.name: value})
        if max_items is None or held_count <= max_items:
            return None
        list_label = f"'{self._node.name}.{container.name}'"
        kept_count = self.offered_item_count(container, {target.name: None})
        if kept_count >= max_items:
            return self._full_message(container, max_items)
        return f"the list {list_label} holds at most {max_items} items, and this would bring it to {held_count}, counting the items inside any connected list."

    def _full_message(self, container: ParameterList, max_items: int) -> str:
        return f"the list '{self._node.name}.{container.name}' is full: it holds at most {max_items} items. Remove some items first."

    def _raise_overrun(self, reason: str) -> None:
        # Workers rebuild one row per item, so values over `max_items` cannot be restored.
        # Only the reason is raised: the set handler names the parameter and wraps it once.
        raise ValueError(reason)

    def row_removed(self, parameter_list: ParameterList, row: BaseNodeElement) -> None:
        """Drop a removed row's value, and tell the node its list changed."""
        self._node.parameter_values.pop(row.name, None)
        self.rows_changed(parameter_list)

    def rows_changed(self, container: ParameterContainer, *, initial_setup: bool = False) -> None:
        """Tell the node a container's rows changed: a row was set, removed or moved.

        Not while loading: whether the rows are what the list holds depends on a connection the load
        may not have made yet, and the list was checked when it was saved.
        """
        if container.name in self._being_replaced or initial_setup:
            return
        # A connected list's before_value_set gets only what its connection delivers, and its return
        # is stored. A row change doesn't set that, so it runs only the validators and after_value_set.
        connected = isinstance(container, ParameterList) and not self.holds_no_value(container)
        self._notify(container, run_before_value_set=not connected)

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
        is rejected, the old rows are put back and the error is raised. Errors give only the reason; the set
        handler names the parameter.

        Raises:
            TypeError: if `items` is not a list.
            ValueError: if there are more items than `max_items`, or a row is connected to another node.
        """
        node = self._node
        list_label = f"'{node.name}.{parameter_list.name}'"
        if not isinstance(items, list):
            msg = f"the list {list_label} can only be set to a list, but got a value of type '{type(items).__name__}'."
            raise TypeError(msg)
        max_items = parameter_list.max_items
        item_count = _item_count(parameter_list, items)
        if max_items is not None and item_count > max_items:
            msg = f"the list {list_label} holds at most {max_items} items, and the new value has {item_count}, counting the items of any list in it."
            raise ValueError(msg)
        old_rows = parameter_list.get_child_parameters()
        if self.has_connected_rows(parameter_list):
            msg = f"some items of the list {list_label} are connected to other nodes, and replacing the items would remove those connections. Disconnect them first."
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
            self._notify(parameter_list)
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

    def _notify(self, container: ParameterContainer, *, run_before_value_set: bool = True) -> None:
        """Run the container's validators and hooks on the value the node now reads, the connected list included.

        Its converters don't run: each row ran them on its own item. The value isn't stored, since
        reading the list builds it again, so what before_value_set returns is unused. A running node
        records it as its result.
        """
        node = self._node
        value = node._get_raw_parameter_value(container.name)
        for validator in container.validators:
            validator(container, value)
        node._also_record_as_result(container, value)
        with self.hooks_running(container.name):
            if run_before_value_set:
                node.before_value_set(parameter=container, value=value)
            with node._reporting_state_set_by("after_value_set"):
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
        result = node._handle_request_in_workflow(
            ListConnectionsForNodeRequest(node_name=node.name, broadcast_result=False)
        )
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
    """How many items `entries` use up of `max_items`, whether they are rows or a connected list's entries."""
    return sum(_counted_items_of_entry(parameter_list, entry) for entry in entries)


def _connected_list_count(parameter_list: ParameterList, connected: Any) -> int:
    """How many items a value connected to the list itself uses up of `max_items`; anything but a list adds none."""
    if not isinstance(connected, list):
        return 0
    return _item_count(parameter_list, connected)


def _counted_items_of_entry(container: ParameterContainer, value: Any) -> int:
    """How many items one row's value, or one entry of a connected list, uses up of `max_items`.

    The items it stores, so `max_items` limits exactly what the node reads. Every `max_items` check
    goes through this.
    """
    return len(_items_of_row(container, value))


def _items_of_row(container: ParameterContainer, value: Any) -> list[Any]:
    """The items one row's value adds to its list.

    A list whose `opens_nested_lists` is set opens lists in the value at every depth and skips `None`,
    the way `get_parameter_list_value` reads it. Any other value is one item.
    """
    if value is None:
        return []
    if isinstance(container, ParameterList) and container.row_value_is_items(value):
        return list(flattened_items(value))
    return [value]


def flattened_items(value: Any) -> Iterator[Any]:
    """The items of `value` with lists at every depth opened and `None` skipped. Strings, bytes and dicts are one item."""
    for item in value:
        if isinstance(item, Iterable) and not isinstance(item, (str, bytes, dict)):
            yield from flattened_items(item)
        elif item is not None:
            yield item
