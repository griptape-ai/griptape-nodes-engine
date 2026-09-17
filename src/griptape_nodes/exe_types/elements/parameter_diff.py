"""Which fields of one parameter differ from another, and what counts as a difference."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.elements.base import BaseNodeElement

if TYPE_CHECKING:
    from griptape_nodes.exe_types.elements.parameter import Parameter


def diff_parameters(parameter: Parameter, other: Parameter) -> dict:
    """Return each field of ``other`` that differs from ``parameter``.

    A dict rather than true or false, because callers alter the fields it names.
    """
    self_dict = parameter.to_dict().copy()
    other_dict = other.to_dict().copy()
    self_dict.pop("next", None)
    self_dict.pop("prev", None)
    self_dict.pop("element_id", None)
    other_dict.pop("next", None)
    other_dict.pop("element_id", None)
    other_dict.pop("prev", None)
    if self_dict == other_dict:
        return {}
    differences = {}
    for key, self_value in self_dict.items():
        other_value = other_dict.get(key, None)
        # handle children here
        if isinstance(self_value, BaseNodeElement) and isinstance(other_value, BaseNodeElement):
            if self_value != other_value:
                differences[key] = other_value
        elif isinstance(self_value, (list, set)) and isinstance(other_value, (list, set)):
            # Through the method, not diff_list_values directly: subclasses override it.
            parameter.check_list(self_value, other_value, differences, key)
        elif self_value != other_value:
            differences[key] = other_value
    return differences


def diff_list_values(self_value: Any, other_value: Any, differences: dict, key: Any) -> None:
    """Record ``other_value`` under ``key`` when the two sequences differ anywhere."""
    # Imported here to avoid a circular import: a parameter is diffed against another parameter.
    from griptape_nodes.exe_types.elements.parameter import Parameter

    # Convert both to lists for index-based iteration
    self_list = list(self_value)
    other_list = list(other_value)
    # Check if they have different lengths
    if len(self_list) != len(other_list):
        differences[key] = other_value
        return
    # Compare each element
    list_differences = False
    for i, item in enumerate(self_list):
        if i >= len(other_list):
            list_differences = True
            break
        # If the element is a Parameter, use its equals method
        if isinstance(item, Parameter) and isinstance(other_list[i], Parameter):
            if item.equals(other_list[i]):  # If there are differences
                list_differences = True
                break
        elif isinstance(item, BaseNodeElement) and isinstance(other_list[i], BaseNodeElement):
            if item != other_list[i]:
                list_differences = True
                break
        # Otherwise use direct comparison
        elif item != other_list[i]:
            list_differences = True
            break
    if list_differences:
        differences[key] = other_value
