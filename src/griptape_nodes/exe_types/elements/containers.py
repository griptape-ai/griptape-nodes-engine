"""Parameters that own other parameters: lists, key-value pairs, and dictionaries."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.elements.base import BaseNodeElement
from griptape_nodes.exe_types.elements.parameter import Parameter
from griptape_nodes.exe_types.elements.parameter_types import ParameterMode, ParameterType

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.trait import Trait


class ParameterContainer(Parameter, ABC):
    """Class managing a container (list/dict/tuple/etc.) of Parameters.

    It is, itself, a Parameter (so it can be the target of compatible Container connections, etc.)
    But it also has the ability to own and manage children and make them accessible by keys, etc.
    """

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        hide: bool | None = None,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            input_types=input_types,
            output_type=output_type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            hide=hide,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_id=element_id,
            element_type=element_type,
        )

    def __bool__(self) -> bool:
        """Parameter containers are always truthy, even when empty.

        This overrides Python's default truthiness behavior for containers with __len__().
        By default, Python makes objects with __len__() falsy when len() == 0, which
        caused bugs where empty ParameterList/ParameterDictionary objects would fail
        'if param' checks and fall back to stale cached values instead of computing
        fresh empty results.

        Unlike standard Python containers, ParameterContainer objects represent
        parameter structure/definitions rather than just data, so they remain
        meaningful even when empty.

        See: https://github.com/griptape-ai/griptape-nodes/issues/1799
        """
        return True

    @abstractmethod
    def add_child_parameter(self) -> Parameter:
        pass


class ParameterList(ParameterContainer):
    _original_traits: set[Trait.__class__ | Trait]

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        hide: bool | None = None,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
        max_items: int | None = None,
        # UI convenience parameters
        collapsed: bool | None = None,
        child_prefix: str | None = None,
        grid: bool | None = None,
        grid_columns: int | None = None,
    ):
        if traits:
            self._original_traits = traits
        else:
            self._original_traits = set()

        self._max_items = max_items
        # Store the UI convenience parameters
        self._collapsed = collapsed
        self._child_prefix = child_prefix
        self._grid = grid
        self._grid_columns = grid_columns

        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            input_types=input_types,
            output_type=output_type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            hide=hide,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_id=element_id,
            element_type=element_type,
        )

    @property
    def max_items(self) -> int | None:
        return self._max_items

    @property
    def collapsed(self) -> bool | None:
        return self._collapsed

    @collapsed.setter
    @BaseNodeElement.emits_update_on_write
    def collapsed(self, value: bool | None) -> None:
        self._collapsed = value

    @property
    def child_prefix(self) -> str | None:
        return self._child_prefix

    @child_prefix.setter
    @BaseNodeElement.emits_update_on_write
    def child_prefix(self, value: str | None) -> None:
        self._child_prefix = value

    @property
    def grid(self) -> bool | None:
        return self._grid

    @grid.setter
    @BaseNodeElement.emits_update_on_write
    def grid(self, value: bool | None) -> None:
        self._grid = value

    @property
    def grid_columns(self) -> int | None:
        return self._grid_columns

    @grid_columns.setter
    @BaseNodeElement.emits_update_on_write
    def grid_columns(self, value: int | None) -> None:
        self._grid_columns = value

    @property
    def ui_options(self) -> dict:
        """Override ui_options to merge convenience parameters in real-time."""
        # Get base ui_options from parent
        base_ui_options = super().ui_options

        # Build convenience options from instance parameters
        convenience_options = {}

        if self._collapsed is not None:
            convenience_options["collapsed"] = self._collapsed

        if self._child_prefix is not None:
            convenience_options["child_prefix"] = self._child_prefix

        if self._grid is not None and self._grid:
            convenience_options["display"] = "grid"

        if self._grid_columns is not None and self._grid:
            convenience_options["columns"] = self._grid_columns

        # Merge convenience options with base ui_options
        return {
            **base_ui_options,
            **convenience_options,
        }

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        """Set ui_options, preserving convenience parameters."""
        # Extract convenience parameters from the incoming value
        if "display" in value and value["display"] == "grid":
            self._grid = True
            if "columns" in value:
                self._grid_columns = value["columns"]
        else:
            self._grid = False

        if "collapsed" in value:
            self._collapsed = value["collapsed"]

        if "child_prefix" in value:
            self._child_prefix = value["child_prefix"]

        # Set the base ui_options (excluding convenience parameters)
        base_ui_options = {
            k: v for k, v in value.items() if k not in ["display", "columns", "collapsed", "child_prefix"]
        }
        self._ui_options = base_ui_options

    def to_dict(self) -> dict[str, Any]:
        """Override to_dict to use the merged ui_options."""
        data = super().to_dict()
        data["ui_options"] = self.ui_options
        return data

    def _custom_getter_for_property_type(self) -> str:
        base_type = super()._custom_getter_for_property_type()
        result = f"list[{base_type}]"
        return result

    def _custom_setter_for_property_type(self, value: str | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.type = value
        super()._custom_setter_for_property_type(value)

    def _custom_setter_for_property_input_types(self, value: list[str] | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.input_types = value
        return super()._custom_setter_for_property_input_types(value)

    def _custom_setter_for_property_output_type(self, value: str | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.output_type = value
        return super()._custom_setter_for_property_output_type(value)

    def _custom_getter_for_property_input_types(self) -> list[str]:
        # For every valid input type, also accept a list variant of that for the CONTAINER Parameter only.
        # Children still use the input types given to them.
        base_input_types = super()._custom_getter_for_property_input_types()
        result = []
        for base_input_type in base_input_types:
            container_variant = f"list[{base_input_type}]"
            result.append(container_variant)

        # Also accept an unparameterized `list`. Sources that build a list at runtime cannot
        # always declare an element type (a mixed image+audio list has no single correct one),
        # so element-type correctness for those is deferred to the consuming node.
        result.append("list")

        return result

    def _custom_getter_for_property_output_type(self) -> str:
        base_type = super()._custom_getter_for_property_output_type()
        result = f"list[{base_type}]"
        return result

    def __len__(self) -> int:
        # Returns the number of child Parameters. Just do the top level.
        param_children = self.find_elements_by_type(element_type=Parameter, find_recursively=False)
        return len(param_children)

    def __getitem__(self, key: int) -> Parameter:
        count = 0
        for child in self._children:
            if isinstance(child, Parameter):
                if count == key:
                    # Found it.
                    return child
                count += 1

        # If we fell out of the for loop, we had a bad value.
        err_str = f"Attempted to get a Parameter List index {key}, which was out of range."
        raise KeyError(err_str)

    def add_child_parameter(self) -> Parameter:
        # Generate a name. This needs to be UNIQUE because children need
        # to be tracked as individuals and not as indices in the list.
        # Ex: a Connection is made to Parameter List[1]. List[0] gets deleted.
        # The OLD List[1] is now List[0], but we need to maintain the Connection
        # to the original entry.
        #
        # (No, we're not renaming it List[0] everywhere for you)
        name = f"{self.name}_ParameterListUniqueParamID_{uuid.uuid4().hex!s}"

        param = Parameter(
            name=name,
            tooltip=self.tooltip,
            type=self._type,
            input_types=self._input_types,
            output_type=self._output_type,
            default_value=self.default_value,
            tooltip_as_input=self.tooltip_as_input,
            tooltip_as_output=self.tooltip_as_output,
            tooltip_as_property=self.tooltip_as_property,
            allowed_modes=self.allowed_modes,
            ui_options=self.ui_options,
            traits=self._original_traits,
            converters=self.converters,
            validators=self.validators,
            settable=self.settable,
            user_defined=True,
            parent_container_name=self.name,
        )

        # Add at the end.
        self.add_child(param)

        return param

    def clear_list(self) -> None:
        """Remove all children that have been added to the list."""
        children = self.find_elements_by_type(element_type=Parameter)
        for child in children:
            if isinstance(child, Parameter):
                self.remove_child(child)
                del child

    # --- Convenience methods for stable list management ---
    def get_child_parameters(self) -> list[Parameter]:
        """Return direct child parameters only, in order of appearance."""
        return self.find_elements_by_type(element_type=Parameter, find_recursively=False)

    def append_child_parameter(self, display_name: str | None = None) -> Parameter:
        """Append one child parameter and optionally set a display name.

        This preserves existing children and adds a new one at the end.
        """
        child = self.add_child_parameter()
        if display_name is not None:
            child.display_name = display_name
        return child

    def remove_last_child_parameter(self) -> None:
        """Remove the last child parameter if one exists.

        This removes from the end to preserve earlier children and their connections.
        """
        children = self.get_child_parameters()
        if children:
            last = children[-1]
            self.remove_child(last)
            del last

    def ensure_length(self, desired_count: int, display_name_prefix: str | None = None) -> None:
        """Grow or shrink the list to the desired length while preserving existing items.

        - If increasing, appends new children to the end.
        - If decreasing, removes children from the end.
        - Optionally sets display names like "{prefix} 1", "{prefix} 2", ...
        """
        if desired_count is None:
            return
        try:
            desired_count = int(desired_count)
        except Exception:
            desired_count = 0
        desired_count = max(desired_count, 0)

        current_children = self.get_child_parameters()
        current_len = len(current_children)

        # Grow
        if current_len < desired_count:
            for index in range(current_len, desired_count):
                name = f"{display_name_prefix} {index + 1}" if display_name_prefix else None
                self.append_child_parameter(display_name=name)

        # Shrink
        elif current_len > desired_count:
            for _ in range(current_len - desired_count):
                self.remove_last_child_parameter()

        # Optionally re-apply display names to existing children to keep indices tidy
        if display_name_prefix:
            for index, child in enumerate(self.get_child_parameters()):
                child.display_name = f"{display_name_prefix} {index + 1}"

    def add_child(self, child: BaseNodeElement) -> None:
        """Override to mark parent node as unresolved when children are added.

        When a ParameterList gains a child parameter, the parent node needs to be
        marked as unresolved to trigger re-evaluation of the node's state and outputs.
        """
        # Validate max_items before adding child
        if self._max_items is not None:
            current_count = len(self._children)
            if current_count >= self._max_items:
                msg = f"Cannot add more items to {self.name}. Maximum {self._max_items} items allowed."
                raise ValueError(msg)

        super().add_child(child)

        # Mark the parent node as unresolved since the parameter structure changed
        if self._node_context is not None:
            # Import at runtime to avoid circular import
            from griptape_nodes.exe_types.node_types import NodeResolutionState

            self._node_context.make_node_unresolved(
                current_states_to_trigger_change_event={NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
            )

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Override to mark parent node as unresolved when children are removed.

        When a ParameterList loses a child parameter, the parent node needs to be
        marked as unresolved to trigger re-evaluation of the node's state and outputs.
        """
        super().remove_child(child)

        # Mark the parent node as unresolved since the parameter structure changed
        if self._node_context is not None:
            # Import at runtime to avoid circular import
            from griptape_nodes.exe_types.node_types import NodeResolutionState

            self._node_context.make_node_unresolved(
                current_states_to_trigger_change_event={NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
            )


class ParameterKeyValuePair(Parameter):
    def __init__(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict],
        # Main parameter options
        type: str | None = None,  # noqa: A002
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        # Key and Value specific options
        key_default_value: Any = None,
        key_tooltip: str | list[dict] | None = None,
        key_ui_options: dict | None = None,
        key_traits: set[Trait.__class__ | Trait] | None = None,
        key_converters: list[Callable[[Any], Any]] | None = None,
        key_validators: list[Callable[[Parameter, Any], None]] | None = None,
        value_default_value: Any = None,
        value_tooltip: str | list[dict] | None = None,
        value_ui_options: dict | None = None,
        value_traits: set[Trait.__class__ | Trait] | None = None,
        value_converters: list[Callable[[Any], Any]] | None = None,
        value_validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_id=element_id,
            element_type=element_type,
        )

        kvp_type = ParameterType.parse_kv_type_pair(self.type)
        if kvp_type is None:
            err_str = f"PropertyKeyValuePair type '{type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)

        # Create key parameter as a child
        key_param = Parameter(
            name=f"{name}.key",
            tooltip=key_tooltip or "Key for the key-value pair",
            type=kvp_type.key_type,
            default_value=key_default_value,
            ui_options=key_ui_options,
            traits=key_traits,
            converters=key_converters,
            validators=key_validators,
        )
        self.add_child(key_param)

        # Create value parameter as a child
        value_param = Parameter(
            name=f"{name}.value",
            tooltip=value_tooltip or "Value for the key-value pair",
            type=kvp_type.value_type,
            default_value=value_default_value,
            ui_options=value_ui_options,
            traits=value_traits,
            converters=value_converters,
            validators=value_validators,
        )
        self.add_child(value_param)

    def _custom_setter_for_property_type(self, value: Any) -> None:
        # Set it as normal.
        super()._custom_setter_for_property_type(value)

        # Ensure this is a valid Key-Value Pair
        base_type = super()._custom_getter_for_property_type()
        kvp_type = ParameterType.parse_kv_type_pair(base_type)
        if kvp_type is None:
            err_str = f"PropertyKeyValuePair type '{base_type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)

        # Update the key and value parameter types
        key_param = self.find_element_by_id(f"{self.name}.key")
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(key_param, Parameter) and isinstance(value_param, Parameter):
            key_param.type = kvp_type.key_type
            value_param.type = kvp_type.value_type

    def get_key(self) -> Any:
        """Get the current value of the key parameter."""
        key_param = self.find_element_by_id(f"{self.name}.key")
        if isinstance(key_param, Parameter):
            return key_param.default_value
        return None

    def set_key(self, value: Any) -> None:
        """Set the value of the key parameter."""
        key_param = self.find_element_by_id(f"{self.name}.key")
        if isinstance(key_param, Parameter):
            key_param.default_value = value

    def get_value(self) -> Any:
        """Get the current value of the value parameter."""
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(value_param, Parameter):
            return value_param.default_value
        return None

    def set_value(self, value: Any) -> None:
        """Set the value of the value parameter."""
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(value_param, Parameter):
            value_param.default_value = value


class ParameterDictionary(ParameterContainer):
    _kvp_type: ParameterType.KeyValueTypePair
    _original_traits: set[Trait.__class__ | Trait]

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_id=element_id,
            element_type=element_type,
        )

        if traits:
            self._original_traits = traits
        else:
            self._original_traits = set()

    def _custom_getter_for_property_type(self) -> str:
        base_type = super()._custom_getter_for_property_type()
        # NOT A TYPO. Internally, we are representing the Dict as a List to preserve the order.
        result = f"list[{base_type}]"
        return result

    def _custom_setter_for_property_type(self, value: Any) -> None:
        # Set it as normal.
        super()._custom_setter_for_property_type(value)

        # We set the type value, now get it back.
        base_type = super()._custom_getter_for_property_type()

        # Ensure this is a valid Key-Value Pair
        base_type = super()._custom_getter_for_property_type()
        kvp_type = ParameterType.parse_kv_type_pair(base_type)
        if kvp_type is None:
            err_str = f"PropertyDictionary type '{base_type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)
        self._kvp_type = kvp_type

    def _custom_getter_for_property_input_types(self) -> list[str]:
        # For every valid input type, also accept a list variant of that for the CONTAINER Parameter only.
        # Children still use the input types given to them.
        base_input_types = super()._custom_getter_for_property_input_types()
        result = []
        for base_input_type in base_input_types:
            container_variant = f"dict[{base_input_type}]"
            result.append(container_variant)

        return result

    def _custom_getter_for_property_output_type(self) -> str:
        base_type = super()._custom_getter_for_property_output_type()
        result = f"dict[{base_type}]"
        return result

    def __len__(self) -> int:
        # Returns the number of child Parameters. Just do the top level.
        param_children = self.find_elements_by_type(element_type=ParameterKeyValuePair, find_recursively=False)
        return len(param_children)

    def __getitem__(self, key: int) -> ParameterKeyValuePair:
        count = 0
        for child in self._children:
            if isinstance(child, ParameterKeyValuePair):
                if count == key:
                    # Found it.
                    return child
                count += 1

        # If we fell out of the for loop, we had a bad value.
        err_str = f"Attempted to get a Parameter Dictionary index {key}, which was out of range."
        raise KeyError(err_str)

    def add_key_value_pair(self) -> ParameterKeyValuePair:
        # Generate a name. This needs to be UNIQUE because children need
        # to be tracked as individuals and not as indices/keys in the dict.
        name = f"{self.name}_ParameterDictUniqueParamID_{uuid.uuid4().hex!s}"

        param = ParameterKeyValuePair(
            name=name,
            tooltip=self.tooltip,
            type=self._type,
            default_value=self.default_value,
            tooltip_as_input=self.tooltip_as_input,
            tooltip_as_output=self.tooltip_as_output,
            tooltip_as_property=self.tooltip_as_property,
            allowed_modes=self.allowed_modes,
            ui_options=self.ui_options,
            traits=self._original_traits,
            converters=self.converters,
            validators=self.validators,
            settable=self.settable,
            user_defined=self.user_defined,
        )

        # Add at the end.
        self.add_child(param)

        return param
