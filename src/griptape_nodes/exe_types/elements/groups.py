"""Elements that lay parameters out together without being parameters themselves."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from griptape_nodes.exe_types.elements.badge import set_initial_badge
from griptape_nodes.exe_types.elements.base import BaseNodeElement
from griptape_nodes.exe_types.elements.parameter import Parameter
from griptape_nodes.exe_types.elements.ui_options import UIOptionsMixin, seed_ui_options

if TYPE_CHECKING:
    from griptape_nodes.exe_types.elements.badge import BadgeData
    from griptape_nodes.exe_types.node_types import BaseNode


class ParameterGroup(BaseNodeElement, UIOptionsMixin):
    """UI element for a group of parameters."""

    def __init__(
        self,
        name: str,
        ui_options: dict | None = None,
        *,
        collapsed: bool = False,
        user_defined: bool = False,
        badge: BadgeData
        | None = None,  # Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
        **kwargs,
    ):
        super().__init__(name=name, **kwargs)
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        # Add collapsed to ui_options if it's True
        if collapsed:
            ui_options["collapsed"] = collapsed

        self._ui_options = ui_options
        self.user_defined = user_defined

        if badge is not None:
            set_initial_badge(self, badge)

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    @property
    def collapsed(self) -> bool:
        """Get whether the parameter group is collapsed.

        Returns:
            True if the group is collapsed, False otherwise
        """
        return self._ui_options.get("collapsed", False)

    @collapsed.setter
    @BaseNodeElement.emits_update_on_write
    def collapsed(self, value: bool) -> None:
        """Set whether the parameter group is collapsed.

        Args:
            value: Whether to collapse the group
        """
        if value:
            self.update_ui_options_key("collapsed", value)
        else:
            ui_options = self._ui_options.copy()
            ui_options.pop("collapsed", None)
            self._ui_options = ui_options

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this node and its children.

        Example:
            {
              "element_id": "container-1",
              "element_type": "ParameterGroup",
              "name": "Group 1",
              "children": [
                {
                    "element_id": "A",
                    "element_type": "Parameter",
                    "children": []
                },
                ...
              ]
            }
        """
        # Get the parent's version first.
        our_dict = super().to_dict()
        # Add in our deltas.
        our_dict["name"] = self.name
        our_dict["ui_options"] = self.ui_options
        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        event_data["ui_options"] = self.ui_options
        return event_data

    def equals(self, other: ParameterGroup) -> dict:
        self_dict = {"name": self.name, "ui_options": self.ui_options}
        other_dict = {"name": other.name, "ui_options": other.ui_options}
        if self_dict == other_dict:
            return {}
        differences = {}
        for key, self_value in self_dict.items():
            other_value = other_dict.get(key)
            if self_value != other_value:
                differences[key] = other_value
        return differences

    def add_child(self, child: BaseNodeElement) -> None:
        child.parent_group_name = self.name
        # Keep parent_element_name in sync with parent_group_name for Parameters.
        # These two fields track the same relationship but have different origins:
        # - parent_group_name: set here by add_child(), always correct
        # - parent_element_name: set by Parameter constructors and handlers, used by
        #   BaseNode.add_parameter() to look up the group, and by cattrs serialization
        # Without this sync, Parameters created via the context manager path (which calls
        # add_child() directly) would have parent_element_name=None, causing them to
        # serialize without their parent group reference and reload as flat/root-level.
        if isinstance(child, Parameter):
            child.parent_element_name = self.name
        return super().add_child(child)

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Clear parent tracking fields (inverse of add_child), then remove from the tree."""
        if isinstance(child, str):
            child_from_str = self.find_element_by_name(child)
            if child_from_str is not None and isinstance(child_from_str, BaseNodeElement):
                child_from_str.parent_group_name = None
                if isinstance(child_from_str, Parameter):
                    child_from_str.parent_element_name = None
                return super().remove_child(child_from_str)
        else:
            child.parent_group_name = None
            if isinstance(child, Parameter):
                child.parent_element_name = None
        return super().remove_child(child)


class ParameterButtonGroup(BaseNodeElement, UIOptionsMixin):
    """UI element for grouping buttons together in a row (similar to shadcn ButtonGroup).

    This class creates a button group container that displays buttons horizontally
    with proper spacing and styling, similar to shadcn/ui's ButtonGroup component.

    Example:
        with ParameterButtonGroup(name="actions", orientation="horizontal") as button_group:
            ParameterButton(
                name="save",
                label="Save",
                variant="default",
            )
            ParameterButton(
                name="cancel",
                label="Cancel",
                variant="secondary",
            )
    """

    def __init__(
        self,
        name: str,
        ui_options: dict | None = None,
        *,
        orientation: Literal["horizontal", "vertical"] = "horizontal",
        hide_label: bool | None = None,
        display_name: str | None = None,
        **kwargs,
    ):
        super().__init__(name=name, element_type="ParameterButtonGroup", **kwargs)

        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        seed_ui_options(self, ui_options, {"hide_label": hide_label, "display_name": display_name})

        # A button group labels itself with its buttons, so the label is hidden unless asked for.
        if "hide_label" not in ui_options:
            ui_options["hide_label"] = True

        ui_options["button_group"] = True
        ui_options["orientation"] = orientation

        self._ui_options = ui_options
        self._orientation: Literal["horizontal", "vertical"] = orientation

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    @property
    def orientation(self) -> Literal["horizontal", "vertical"]:
        """Get the button group orientation.

        Returns:
            "horizontal" for buttons in a row, "vertical" for buttons in a column
        """
        return self._orientation

    @orientation.setter
    @BaseNodeElement.emits_update_on_write
    def orientation(self, value: Literal["horizontal", "vertical"]) -> None:
        """Set the button group orientation.

        Args:
            value: "horizontal" for buttons in a row, "vertical" for buttons in a column
        """
        self._orientation = value
        self.update_ui_options_key("orientation", value)

    @property
    def hide_label(self) -> bool:
        """Whether the button group label is hidden in the UI (defaults to True)."""
        return self.ui_options.get("hide_label", True)

    @hide_label.setter
    @BaseNodeElement.emits_update_on_write
    def hide_label(self, value: bool) -> None:
        self.update_ui_options_key("hide_label", value)

    @property
    def display_name(self) -> str | None:
        """Human-readable label for the button group (overrides the default from `name`)."""
        return self.ui_options.get("display_name")

    @display_name.setter
    @BaseNodeElement.emits_update_on_write
    def display_name(self, value: str | None) -> None:
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("display_name", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("display_name", value)

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this button group and its children."""
        our_dict = super().to_dict()
        our_dict["name"] = self.name
        our_dict["ui_options"] = self.ui_options
        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        event_data["ui_options"] = self.ui_options
        return event_data

    def add_child(self, child: BaseNodeElement) -> None:
        child.parent_group_name = self.name
        if isinstance(child, Parameter):
            child.parent_element_name = self.name
        return super().add_child(child)

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Clear parent tracking fields (inverse of add_child), then remove from the tree."""
        if isinstance(child, str):
            child_from_str = self.find_element_by_name(child)
            if child_from_str is not None and isinstance(child_from_str, BaseNodeElement):
                child_from_str.parent_group_name = None
                if isinstance(child_from_str, Parameter):
                    child_from_str.parent_element_name = None
                return super().remove_child(child_from_str)
        else:
            child.parent_group_name = None
            if isinstance(child, Parameter):
                child.parent_element_name = None
        return super().remove_child(child)
