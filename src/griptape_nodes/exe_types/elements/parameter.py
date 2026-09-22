"""The parameter itself: what it accepts, how it is displayed, what it emits, and how two differ."""

from __future__ import annotations

import logging
import uuid
import warnings
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import field
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.elements.badge import set_initial_badge
from griptape_nodes.exe_types.elements.base import BaseNodeElement
from griptape_nodes.exe_types.elements.parameter_types import (
    ParameterMode,
    ParameterType,
    ParameterTypeBuiltin,
    accepts_incoming_type,
    canonical_type_name,
    disallowed_mode_flags,
    modes_from_flags,
    modes_with,
)
from griptape_nodes.exe_types.elements.tooltips import default_parameter_tooltip
from griptape_nodes.exe_types.elements.trait import Trait, instantiate_trait
from griptape_nodes.exe_types.elements.ui_options import UIOptionsMixin, seed_ui_options
from griptape_nodes.exe_types.trait_state import TraitStateEntry, as_saved_state_value, changed_trait_states

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.badge import BadgeData
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


class ParameterBase(BaseNodeElement, ABC):
    @property
    @abstractmethod
    def tooltip(self) -> str | list[dict]:
        """Get the default tooltip for this Parameter-like object.

        Returns:
            str | list[dict]: Either the explicit tooltip string or a list of dicts for special UI handling.
        """

    @tooltip.setter
    @abstractmethod
    def tooltip(self, value: str | list[dict]) -> None:
        pass

    @abstractmethod
    def get_default_value(self) -> Any:
        """Get the default value that should be assigned to this Parameter-like object.

        Returns:
            Any: The default value to assign when initialized or reset.
        """

    @abstractmethod
    def get_input_types(self) -> list[str] | None:
        """Get the list of input types this Parameter-like object accepts, or None if it doesn't accept any.

        Returns:
            list[str] | None: List of user-defined types supported.
        """

    @abstractmethod
    def get_output_type(self) -> str | None:
        """Get the output type this Parameter-like object emits, or None if it doesn't output.

        Returns:
            str | None: User-defined type output.
        """

    @abstractmethod
    def get_type(self) -> str | None:
        pass

    @abstractmethod
    def get_tooltip_as_input(self) -> str | list[dict] | None:
        pass


class Parameter(BaseNodeElement, UIOptionsMixin):
    # This is the list of types that the Parameter can accept, either externally or when internally treated as a property.
    # Today, we can accept multiple types for input, but only a single output type.
    tooltip: str | list[dict]  # Default tooltip, can be string or list of dicts
    default_value: Any = None
    _input_types: list[str] | None
    _output_type: str | None
    _type: str | None
    tooltip_as_input: str | list[dict] | None = None
    tooltip_as_property: str | list[dict] | None = None
    tooltip_as_output: str | list[dict] | None = None

    # "settable" here means whether it can be assigned to during regular business operation.
    # During save/load, this value IS still serialized to save its proper state.
    _settable: bool = True

    # "serializable" controls whether parameter values should be serialized during save/load operations.
    # Set to False for parameters containing non-serializable types (ImageDrivers, PromptDrivers, file handles, etc.)
    serializable: bool = True

    user_defined: bool = False
    private: bool = False
    exclude_from_metadata: bool = False
    allow_variable_substitution: bool = True
    _allowed_modes: set = field(
        default_factory=lambda: {
            ParameterMode.OUTPUT,
            ParameterMode.INPUT,
            ParameterMode.PROPERTY,
        }
    )
    _converters: list[Callable[[Any], Any]]
    _validators: list[Callable[[Parameter, Any], None]]
    _on_incoming_connection_removed: list[Callable[[Parameter, str, str], None]]
    _on_outgoing_connection_removed: list[Callable[[Parameter, str, str], None]]
    _ui_options: dict
    next: Parameter | None = None
    prev: Parameter | None = None
    parent_container_name: str | None = None
    parent_element_name: str | None = None

    def __init__(  # noqa: C901, PLR0912, PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict] | None = None,
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,  # We are going to make these children.
        ui_options: dict | None = None,
        *,
        hide: bool | None = None,
        hide_label: bool | None = None,
        hide_property: bool | None = None,
        display_name: str | None = None,
        allow_input: bool = True,
        allow_property: bool = True,
        allow_output: bool = True,
        settable: bool = True,
        serializable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        allow_variable_substitution: bool = True,
        element_id: str | None = None,
        element_type: str | None = None,
        parent_container_name: str | None = None,
        parent_element_name: str | None = None,
        badge: BadgeData
        | None = None,  # Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
    ):
        if not element_id:
            element_id = str(uuid.uuid4().hex)
        if not element_type:
            element_type = self.__class__.__name__

        # Set parent references BEFORE super().__init__(), which triggers __post_init__().
        # If this Parameter is being created inside a ParameterGroup context manager,
        # __post_init__() will call add_child() which overwrites these with the correct
        # group name. Setting them first ensures they exist as attributes, and the context
        # manager path gets the final say.
        self.parent_container_name = parent_container_name
        self.parent_element_name = parent_element_name

        super().__init__(element_id=element_id, element_type=element_type)
        self.name = name

        # Generate default tooltip if none provided
        if not tooltip:
            tooltip = default_parameter_tooltip(name, type, input_types, output_type)

        self.tooltip = tooltip
        self.default_value = default_value
        self.tooltip_as_input = tooltip_as_input
        self.tooltip_as_property = tooltip_as_property
        self.tooltip_as_output = tooltip_as_output
        self._settable = settable
        self.serializable = serializable
        self.user_defined = user_defined
        self.private = private
        self.exclude_from_metadata = exclude_from_metadata
        self.allow_variable_substitution = allow_variable_substitution

        # Process allowed_modes - use convenience parameters if allowed_modes not explicitly set
        if allowed_modes is None:
            self._allowed_modes = modes_from_flags(
                allow_input=allow_input, allow_property=allow_property, allow_output=allow_output
            )
        else:
            self._allowed_modes = allowed_modes

            # Warn if both allowed_modes and convenience parameters are set
            convenience_params_used = disallowed_mode_flags(
                allow_input=allow_input, allow_property=allow_property, allow_output=allow_output
            )

            if convenience_params_used:
                warnings.warn(
                    f"Parameter '{name}': Both 'allowed_modes' and convenience parameters "
                    f"({', '.join(convenience_params_used)}) are set. Using 'allowed_modes' "
                    f"and ignoring convenience parameters.",
                    UserWarning,
                    stacklevel=2,
                )

        if converters is None:
            self._converters = []
        else:
            self._converters = converters

        if validators is None:
            self._validators = []
        else:
            self._validators = validators

        self._on_incoming_connection_removed = []
        self._on_outgoing_connection_removed = []

        # Process common UI options from constructor parameters
        if ui_options is None:
            self._ui_options = {}
        else:
            self._ui_options = ui_options.copy()

        # Validate that explicit parameters don't conflict with ui_options, then add the ones
        # ui_options did not already name.
        seed_ui_options(
            self,
            self._ui_options,
            {
                "hide": hide,
                "hide_label": hide_label,
                "hide_property": hide_property,
                "display_name": display_name,
            },
        )
        if traits:
            for trait in traits:
                # Add a trait as a child
                # UI options are now traits! sorry!
                self.add_child(instantiate_trait(trait))
        if badge is not None:
            set_initial_badge(self, badge)
        self.type = type
        self.input_types = input_types
        self.output_type = output_type

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this node and its children."""
        # Get the parent's version first.
        our_dict = super().to_dict()
        # Add in our deltas.
        our_dict["name"] = self.name
        our_dict["type"] = self.type
        our_dict["input_types"] = self.input_types
        our_dict["output_type"] = self.output_type
        our_dict["default_value"] = self.default_value
        our_dict["tooltip"] = self.tooltip
        our_dict["tooltip_as_input"] = self.tooltip_as_input
        our_dict["tooltip_as_output"] = self.tooltip_as_output
        our_dict["tooltip_as_property"] = self.tooltip_as_property

        our_dict["is_user_defined"] = self.user_defined
        our_dict["settable"] = self.settable
        our_dict["serializable"] = self.serializable
        our_dict["private"] = self.private
        our_dict["exclude_from_metadata"] = self.exclude_from_metadata
        our_dict["allow_variable_substitution"] = self.allow_variable_substitution
        our_dict["ui_options"] = self.ui_options

        # Let's bundle up the mode details.
        allows_input = ParameterMode.INPUT in self.allowed_modes
        allows_property = ParameterMode.PROPERTY in self.allowed_modes
        allows_output = ParameterMode.OUTPUT in self.allowed_modes
        our_dict["mode_allowed_input"] = allows_input
        our_dict["mode_allowed_property"] = allows_property
        our_dict["mode_allowed_output"] = allows_output
        our_dict["parent_container_name"] = self.parent_container_name
        our_dict["parent_element_name"] = self.parent_element_name
        our_dict["parent_group_name"] = self.parent_group_name

        return our_dict

    def trait_states(self) -> list[dict[str, Any]]:
        """Return save-only trait identity and plain-data state."""
        states: list[dict[str, Any]] = []
        for trait in self.find_elements_by_type(Trait):
            trait_state: dict[str, Any] = {}
            for key, value in trait.to_state().items():
                saved = as_saved_state_value(value)
                if saved.unsupported_type is not None:
                    logger.warning(
                        "Trait '%s' holds a %s in '%s', which cannot be written to a saved workflow. "
                        "The parameter will load without this value.",
                        type(trait).__name__,
                        saved.unsupported_type,
                        key,
                    )
                    continue
                trait_state[key] = saved.value
            entry = TraitStateEntry(trait_name=type(trait).__name__, trait_state=trait_state)
            states.append(entry.to_dict())
        return states

    def save_dict(self) -> dict[str, Any]:
        """Return ``to_dict()`` with trait-owned and code-only fields in their saved form.

        ``equals()`` and ``NodeManager`` both need this view, and it has to be the same view
        in both places or a diff-based save writes the wrong thing.
        """
        our_dict = self.to_dict()
        # Trait-derived keys belong to the trait, not the parameter: ``traits`` below carries
        # them, so the merged ``ui_options`` would duplicate them as stored options.
        our_dict["ui_options"] = self.authored_ui_options()
        our_dict["traits"] = self.trait_states()
        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_dict = self.to_dict()
        event_data = super().to_event(node)
        event_dict.update(event_data)
        # Update for our name with the right values
        name = event_dict.pop("name")
        event_dict["parameter_name"] = name
        # Update with value
        if node is not None:
            event_dict["value"] = node.get_parameter_value(self.name)
        return event_dict

    @property
    def type(self) -> str:
        return self._custom_getter_for_property_type()

    def _custom_getter_for_property_type(self) -> str:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._type:
            return self._type
        if self._input_types:
            return self._input_types[0]
        if self._output_type:
            return self._output_type
        return ParameterTypeBuiltin.STR.value

    @type.setter  # noqa: A003
    @BaseNodeElement.emits_update_on_write
    def type(self, value: str | None) -> None:
        self._custom_setter_for_property_type(value)

    def _custom_setter_for_property_type(self, value: str | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is None:
            self._type = None
            return
        self._type = canonical_type_name(value)

    @property
    def converters(self) -> list[Callable[[Any], Any]]:
        converters = []
        traits = self.find_elements_by_type(Trait)
        for trait in traits:
            converters += trait.converters_for_trait()
        converters += self._converters
        return converters

    @property
    def validators(self) -> list[Callable[[Parameter, Any], None]]:
        validators = []
        traits = self.find_elements_by_type(Trait)  # TODO: https://github.com/griptape-ai/griptape-nodes/issues/857
        for trait in traits:
            validators += trait.validators_for_trait()
        validators += self._validators
        return validators

    @property
    def has_directly_attached_converters(self) -> bool:
        """The directly-attached converter list is non-empty.

        Trait-derived converters are merged in by the ``converters``
        getter; this checks only what was passed to ``__init__`` or
        appended directly.
        """
        return bool(self._converters)

    @property
    def has_directly_attached_validators(self) -> bool:
        """The directly-attached validator list is non-empty.

        Trait-derived validators are merged in by the ``validators``
        getter; this checks only what was passed to ``__init__`` or
        appended directly.
        """
        return bool(self._validators)

    @property
    def has_traits(self) -> bool:
        """Any Trait child is attached.

        Walks ``find_elements_by_type(Trait)`` because traits are stored
        as child node-elements, not in a flat list. Cheap at probe
        scale (called once per parameter at library load).
        """
        return bool(self.find_elements_by_type(Trait))

    @property
    def on_incoming_connection_removed(self) -> list[Callable[[Parameter, str, str], None]]:
        return self._on_incoming_connection_removed

    @property
    def on_outgoing_connection_removed(self) -> list[Callable[[Parameter, str, str], None]]:
        return self._on_outgoing_connection_removed

    @property
    def allowed_modes(self) -> set[ParameterMode]:
        return self._allowed_modes

    @allowed_modes.setter
    @BaseNodeElement.emits_update_on_write
    def allowed_modes(self, value: Any) -> None:
        self._allowed_modes = value
        # Handle mode flag decomposition
        if isinstance(value, set):
            self._changes["mode_allowed_input"] = ParameterMode.INPUT in value
            self._changes["mode_allowed_output"] = ParameterMode.OUTPUT in value
            self._changes["mode_allowed_property"] = ParameterMode.PROPERTY in value

    @property
    def settable(self) -> bool:
        return self._settable

    @settable.setter
    @BaseNodeElement.emits_update_on_write
    def settable(self, value: bool) -> None:
        self._settable = value

    @property
    def ui_options(self) -> dict:
        """Overlay trait-rendered options on stored options.

        Trait state wins over stale stored copies. Only authored options are persisted.
        """
        ui_options = self.authored_ui_options()
        for trait in self.find_elements_by_type(Trait):
            ui_options = ui_options | trait.ui_options_for_trait()
        return ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        """Route trait-owned keys to the traits that render them, then store the write as given.

        Every write lands here: node code, the editor, a saved file, and ``update_ui_options``.
        Keeping the flat write stored means detaching a trait reveals the written value.
        """
        self._adopt_trait_options(value)
        self._ui_options = value

    def _adopt_trait_options(self, value: dict) -> None:
        for trait in self.find_elements_by_type(Trait):
            adopted = trait.state_from_ui_options(value)
            if not adopted:
                self._report_unadopted_trait_options(trait, value)
                continue
            try:
                trait.apply_state(adopted)
            except (TypeError, ValueError):
                logger.warning(
                    "Attempted to update the %s control on parameter '%s' from a UI option change, "
                    "but it would not accept those values, so the control is unchanged.",
                    type(trait).__name__,
                    self.name,
                )

    def _report_unadopted_trait_options(self, trait: Trait, value: dict) -> None:
        """Report a write the trait renders over, which is neither applied nor saved.

        A write matching what the trait already renders is the editor echoing it back, and
        changes nothing.
        """
        ignored = sorted(
            key for key, rendered in trait.ui_options_for_trait().items() if key in value and value[key] != rendered
        )
        if not ignored:
            return
        logger.warning(
            "Attempted to set %s on parameter '%s', but its %s control renders those keys and does "
            "not read them back, so the change has no effect and is not saved. Set the control's own "
            "state instead, or give the control a 'state_from_ui_options' that accepts these keys.",
            ", ".join(f"'{key}'" for key in ignored),
            self.name,
            type(trait).__name__,
        )

    def remove_ui_options_key(self, key: str) -> None:
        """Remove a stored option, or report that a trait renders it regardless.

        A trait-rendered key is never stored raw, so there is no copy to remove: only the
        trait's own state controls it.
        """
        for trait in self.find_elements_by_type(Trait):
            if key in trait.ui_options_for_trait():
                self._report_unremovable_trait_option(trait, key)
                return
        super().remove_ui_options_key(key)

    def _report_unremovable_trait_option(self, trait: Trait, key: str) -> None:
        logger.warning(
            "Attempted to remove '%s' from parameter '%s', but its %s control renders that key "
            "regardless, so removing it here has no effect. Change the control's own state "
            "instead, or detach it.",
            key,
            self.name,
            type(trait).__name__,
        )

    def authored_ui_options(self) -> dict[str, Any]:
        """Remove options rendered by attached traits.

        Filtering on read handles keys written before or after trait attachment while
        preserving stored values if the trait is detached.
        """
        return self._without_trait_owned_keys(super().authored_ui_options())

    def _without_trait_owned_keys(self, value: dict) -> dict:
        trait_owned: set[str] = set()
        for trait in self.find_elements_by_type(Trait):
            trait_owned.update(trait.ui_options_for_trait())
        return {key: option for key, option in value.items() if key not in trait_owned}

    @property
    def hide(self) -> bool:
        """Get whether the entire parameter is hidden in the UI.

        Returns:
            True if the parameter should be hidden, False otherwise
        """
        return self.ui_options.get("hide", False)

    @hide.setter
    @BaseNodeElement.emits_update_on_write
    def hide(self, value: bool) -> None:
        """Set whether to hide the entire parameter in the UI.

        Args:
            value: True to hide the parameter, False to show it
        """
        self.update_ui_options_key("hide", value)

    @property
    def hide_label(self) -> bool:
        """Get whether the parameter label is hidden in the UI.

        Returns:
            True if the label should be hidden, False otherwise
        """
        return self.ui_options.get("hide_label", False)

    @hide_label.setter
    @BaseNodeElement.emits_update_on_write
    def hide_label(self, value: bool) -> None:
        """Set whether to hide the parameter label in the UI.

        Args:
            value: True to hide the label, False to show it
        """
        self.update_ui_options_key("hide_label", value)

    @property
    def hide_property(self) -> bool:
        """Get whether the parameter is hidden in property mode.

        Returns:
            True if the parameter should be hidden in property mode, False otherwise
        """
        return self.ui_options.get("hide_property", False)

    @hide_property.setter
    @BaseNodeElement.emits_update_on_write
    def hide_property(self, value: bool) -> None:
        """Set whether to hide the parameter in property mode.

        Args:
            value: True to hide in property mode, False to show it
        """
        self.update_ui_options_key("hide_property", value)

    @property
    def display_name(self) -> str | None:
        """Get the display name override for the parameter.

        Returns:
            The display name if set, None to use the default (parameter name)
        """
        return self.ui_options.get("display_name")

    @display_name.setter
    @BaseNodeElement.emits_update_on_write
    def display_name(self, value: str | None) -> None:
        """Set the display name override for the parameter.

        Args:
            value: Display name string, or None to use the default (parameter name)
        """
        if value is None:
            self.remove_ui_options_key("display_name")
        else:
            self.update_ui_options_key("display_name", value)

    @property
    def allow_input(self) -> bool:
        """Get whether the parameter allows INPUT mode.

        Returns:
            True if INPUT mode is allowed, False otherwise
        """
        return ParameterMode.INPUT in self.allowed_modes

    @allow_input.setter
    def allow_input(self, value: bool) -> None:
        """Set whether to allow INPUT mode.

        Args:
            value: True to allow INPUT mode, False to disallow it
        """
        self.allowed_modes = modes_with(self.allowed_modes, ParameterMode.INPUT, allowed=value)

    @property
    def allow_property(self) -> bool:
        """Get whether the parameter allows PROPERTY mode.

        Returns:
            True if PROPERTY mode is allowed, False otherwise
        """
        return ParameterMode.PROPERTY in self.allowed_modes

    @allow_property.setter
    def allow_property(self, value: bool) -> None:
        """Set whether to allow PROPERTY mode.

        Args:
            value: True to allow PROPERTY mode, False to disallow it
        """
        self.allowed_modes = modes_with(self.allowed_modes, ParameterMode.PROPERTY, allowed=value)

    @property
    def allow_output(self) -> bool:
        """Get whether the parameter allows OUTPUT mode.

        Returns:
            True if OUTPUT mode is allowed, False otherwise
        """
        return ParameterMode.OUTPUT in self.allowed_modes

    @allow_output.setter
    def allow_output(self, value: bool) -> None:
        """Set whether to allow OUTPUT mode.

        Args:
            value: True to allow OUTPUT mode, False to disallow it
        """
        self.allowed_modes = modes_with(self.allowed_modes, ParameterMode.OUTPUT, allowed=value)

    @property
    def input_types(self) -> list[str]:
        return self._custom_getter_for_property_input_types()

    def _custom_getter_for_property_input_types(self) -> list[str]:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._input_types:
            return self._input_types
        if self._type:
            return [self._type]
        if self._output_type:
            return [self._output_type]
        return [ParameterTypeBuiltin.STR.value]

    @input_types.setter
    @BaseNodeElement.emits_update_on_write
    def input_types(self, value: list[str] | None) -> None:
        self._custom_setter_for_property_input_types(value)

    def _custom_setter_for_property_input_types(self, value: list[str] | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is None:
            self._input_types = None
        else:
            self._input_types = [canonical_type_name(new_type) for new_type in value]

    @property
    def output_type(self) -> str:
        return self._custom_getter_for_property_output_type()

    def _custom_getter_for_property_output_type(self) -> str:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._output_type:
            # If an output type was specified, use that.
            return self._output_type
        if self._type:
            # Otherwise, see if we have a list of input_types. If so, use the first one.
            return self._type

        # Otherwise, see if we have a list of input_types. If so, use the first one.
        if self._input_types:
            return self._input_types[0]
        # Otherwise, return a string.
        return ParameterTypeBuiltin.STR.value

    @output_type.setter
    @BaseNodeElement.emits_update_on_write
    def output_type(self, value: str | None) -> None:
        self._custom_setter_for_property_output_type(value)

    def _custom_setter_for_property_output_type(self, value: str | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is None:
            self._output_type = None
            return
        self._output_type = canonical_type_name(value)

    def add_trait(self, trait: type[Trait] | Trait) -> None:
        self.add_child(instantiate_trait(trait))

    def remove_trait(self, trait_type: BaseNodeElement) -> None:
        # You are NOT ALLOWED TO ADD DUPLICATE TRAITS (kate)
        self.remove_child(trait_type)

    def add_converter(self, converter: Callable[[Any], Any]) -> None:
        """Append a converter to this parameter's directly-attached converter list.

        Trait converters run BEFORE directly-attached ones (see the
        ``converters`` property), so a converter added here observes the
        value only after every attached trait has already converted it.
        """
        self._converters.append(converter)

    def is_incoming_type_allowed(self, incoming_type: str | None) -> bool:
        return accepts_incoming_type(self.input_types, incoming_type)

    def is_outgoing_type_allowed(self, target_type: str | None) -> bool:
        return ParameterType.are_types_compatible(source_type=self.output_type, target_type=target_type)

    @BaseNodeElement.emits_update_on_write
    def set_default_value(self, value: Any) -> None:
        self.default_value = value

    def get_mode(self) -> set:
        return self.allowed_modes

    def add_mode(self, mode: ParameterMode) -> None:
        self.allowed_modes.add(mode)

    def remove_mode(self, mode: ParameterMode) -> None:
        self.allowed_modes.remove(mode)

    def copy(self) -> Parameter:
        param = deepcopy(self)
        param.next = None
        param.prev = None
        return param

    def check_list(self, self_value: Any, other_value: Any, differences: dict, key: Any) -> None:
        """How this parameter diffs two sequences. Override to change what counts as a difference."""
        diff_list_values(self_value, other_value, differences, key)

    # intentionally not overwriting __eq__ because I want to return a dict not true or false
    def equals(self, other: Parameter) -> dict:
        return diff_parameters(self, other)


def diff_parameters(parameter: Parameter, other: Parameter) -> dict:
    """Return each field of ``other`` that differs from ``parameter``.

    A dict rather than true or false, because callers alter the fields it names.
    """
    self_dict = parameter.save_dict()
    other_dict = other.save_dict()
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
        if key == "traits":
            if self_value != other_value:
                differences[key] = changed_trait_states(self_value, other_dict["traits"])
        # handle children here
        elif isinstance(self_value, BaseNodeElement) and isinstance(other_value, BaseNodeElement):
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
