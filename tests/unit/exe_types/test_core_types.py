from unittest.mock import ANY

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import (
    BadgeData,
    BaseNodeElement,
    NodeMessagePayload,
    Parameter,
    ParameterButtonGroup,
    ParameterGroup,
    ParameterList,
    ParameterType,
)

# No badge by default; elements send badge: null until set_badge() is called.
NO_BADGE = None


class TestBaseNodeElement:
    @pytest.fixture
    def ui_element(self) -> BaseNodeElement:
        with BaseNodeElement() as root:
            with BaseNodeElement():
                BaseNodeElement()
                with ParameterGroup(name="group1"):
                    BaseNodeElement(element_id="leaf1")
            with BaseNodeElement():
                BaseNodeElement(element_id="leaf2")
                Parameter(
                    element_id="parameter",
                    name="test",
                    input_types=["str"],
                    type="str",
                    output_type="str",
                    tooltip="test",
                )

        return root

    def test_init(self) -> None:
        assert BaseNodeElement()

        with BaseNodeElement() as root:
            child = BaseNodeElement()

            assert root._children == [child]

    def test__enter__(self) -> None:
        with BaseNodeElement() as ui:
            assert ui

    def test__repr__(self) -> None:
        assert repr(BaseNodeElement()) == "BaseNodeElement(self.children=[])"

    def test_to_dict(self, ui_element: BaseNodeElement) -> None:
        assert ui_element.to_dict() == {
            "element_id": ANY,
            "element_type": "BaseNodeElement",
            "parent_group_name": None,
            "badge": NO_BADGE,
            "children": [
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": ANY,
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "element_id": ANY,
                            "element_type": "ParameterGroup",
                            "name": "group1",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "ui_options": {},
                            "children": [
                                {
                                    "element_id": "leaf1",
                                    "element_type": "BaseNodeElement",
                                    "parent_group_name": "group1",
                                    "badge": NO_BADGE,
                                    "children": [],
                                }
                            ],
                        },
                    ],
                },
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": "leaf2",
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "element_id": "parameter",
                            "element_type": "Parameter",
                            "children": [],
                            "default_value": None,
                            "input_types": [
                                "str",
                            ],
                            "is_user_defined": False,
                            "mode_allowed_input": True,
                            "mode_allowed_output": True,
                            "mode_allowed_property": True,
                            "name": "test",
                            "parent_group_name": None,
                            "output_type": "str",
                            "tooltip": "test",
                            "tooltip_as_input": None,
                            "tooltip_as_output": None,
                            "tooltip_as_property": None,
                            "type": "str",
                            "settable": True,
                            "serializable": True,
                            "private": False,
                            "allow_variable_substitution": True,
                            "ui_options": {},
                            "parent_container_name": None,
                            "parent_element_name": None,
                            "badge": NO_BADGE,
                        },
                    ],
                },
            ],
        }

    def test_add_child(self, ui_element: BaseNodeElement) -> None:
        found_element = ui_element.find_element_by_id("leaf1")
        assert found_element is not None
        found_element.add_child(BaseNodeElement(element_id="leaf3"))

        assert ui_element.to_dict() == {
            "element_id": ANY,
            "element_type": "BaseNodeElement",
            "parent_group_name": None,
            "badge": NO_BADGE,
            "children": [
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": ANY,
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "element_id": ANY,
                            "element_type": "ParameterGroup",
                            "name": "group1",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "ui_options": {},
                            "children": [
                                {
                                    "element_id": "leaf1",
                                    "element_type": "BaseNodeElement",
                                    "parent_group_name": "group1",
                                    "badge": NO_BADGE,
                                    "children": [
                                        {
                                            "element_id": "leaf3",
                                            "element_type": "BaseNodeElement",
                                            "parent_group_name": None,
                                            "badge": NO_BADGE,
                                            "children": [],
                                        },
                                    ],
                                }
                            ],
                        },
                    ],
                },
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": "leaf2",
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "children": [],
                            "default_value": None,
                            "element_id": "parameter",
                            "element_type": "Parameter",
                            "input_types": [
                                "str",
                            ],
                            "is_user_defined": False,
                            "mode_allowed_input": True,
                            "mode_allowed_output": True,
                            "mode_allowed_property": True,
                            "name": "test",
                            "parent_group_name": None,
                            "output_type": "str",
                            "tooltip": "test",
                            "tooltip_as_input": None,
                            "tooltip_as_output": None,
                            "tooltip_as_property": None,
                            "type": "str",
                            "settable": True,
                            "serializable": True,
                            "private": False,
                            "allow_variable_substitution": True,
                            "ui_options": {},
                            "parent_container_name": None,
                            "parent_element_name": None,
                            "badge": NO_BADGE,
                        },
                    ],
                },
            ],
        }

    def test_find_element_by_id(self, ui_element: BaseNodeElement) -> None:
        element = ui_element.find_element_by_id("leaf1")
        assert element is not None
        assert element.element_id == "leaf1"

        element = ui_element.find_element_by_id("leaf2")
        assert element is not None
        assert element.element_id == "leaf2"

    @pytest.mark.parametrize(("element_type", "num_expected"), [(BaseNodeElement, 7), (Parameter, 1)])
    def test_find_elements_by_type(self, ui_element: BaseNodeElement, element_type: type, num_expected: int) -> None:
        elements = ui_element.find_elements_by_type(element_type)
        assert len(elements) == num_expected

    def test_remove_child(self, ui_element: BaseNodeElement) -> None:
        element_to_remove = ui_element.find_element_by_id("leaf1")

        assert element_to_remove is not None

        ui_element.remove_child(element_to_remove)

        assert ui_element.to_dict() == {
            "element_id": ANY,
            "element_type": "BaseNodeElement",
            "parent_group_name": None,
            "badge": NO_BADGE,
            "children": [
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": ANY,
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "element_id": ANY,
                            "element_type": "ParameterGroup",
                            "name": "group1",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "ui_options": {},
                            "children": [],
                        },
                    ],
                },
                {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "parent_group_name": None,
                    "badge": NO_BADGE,
                    "children": [
                        {
                            "element_id": "leaf2",
                            "element_type": "BaseNodeElement",
                            "parent_group_name": None,
                            "badge": NO_BADGE,
                            "children": [],
                        },
                        {
                            "element_id": "parameter",
                            "element_type": "Parameter",
                            "children": [],
                            "default_value": None,
                            "input_types": [
                                "str",
                            ],
                            "is_user_defined": False,
                            "mode_allowed_input": True,
                            "mode_allowed_output": True,
                            "mode_allowed_property": True,
                            "name": "test",
                            "parent_group_name": None,
                            "output_type": "str",
                            "tooltip": "test",
                            "tooltip_as_input": None,
                            "tooltip_as_output": None,
                            "tooltip_as_property": None,
                            "type": "str",
                            "settable": True,
                            "serializable": True,
                            "private": False,
                            "allow_variable_substitution": True,
                            "ui_options": {},
                            "parent_container_name": None,
                            "parent_element_name": None,
                            "badge": NO_BADGE,
                        },
                    ],
                },
            ],
        }

    def test_get_current(self) -> None:
        with BaseNodeElement() as ui:
            assert ui
            assert ui.get_current() == ui
        assert ui.get_current() is None


class TestBadgeData:
    def test_to_dict_includes_icon_and_color_when_set(self) -> None:
        badge = BadgeData(
            variant="info",
            message="Drop files here",
            icon="upload-cloud",
            color="#3b82f6",
        )
        result = badge.to_dict()
        assert result["variant"] == "info"
        assert result["message"] == "Drop files here"
        assert result["icon"] == "upload-cloud"
        assert result["color"] == "#3b82f6"

    def test_to_dict_omits_icon_and_color_when_not_set(self) -> None:
        badge = BadgeData(variant="info", message="Info message")
        result = badge.to_dict()
        assert "icon" not in result
        assert "color" not in result

    def test_set_badge_with_icon_and_color(self) -> None:
        element = BaseNodeElement()
        element.set_badge(
            variant="info",
            message="Upload",
            icon="upload-cloud",
            color="#3b82f6",
        )
        badge = element.get_badge()
        assert badge is not None
        assert badge.variant == "info"
        assert badge.icon == "upload-cloud"
        assert badge.color == "#3b82f6"
        assert badge.to_dict()["icon"] == "upload-cloud"
        assert badge.to_dict()["color"] == "#3b82f6"

    def test_apply_badge_from_message_data_with_icon_and_color(self) -> None:
        element = BaseNodeElement()
        element.set_badge(variant="info", message="Initial")
        payload = NodeMessagePayload(
            data={
                "variant": "info",
                "message": "Updated",
                "icon": "upload-cloud",
                "color": "#3b82f6",
            }
        )
        result = element.on_message_received("set_badge", payload)
        assert result is not None
        assert result.success is True
        badge = element.get_badge()
        assert badge is not None
        assert badge.message == "Updated"
        assert badge.icon == "upload-cloud"
        assert badge.color == "#3b82f6"

    def test_parameter_with_badge_icon_and_color(self) -> None:
        badge = BadgeData(
            variant="info",
            message="Drop files here",
            icon="upload-cloud",
            color="#3b82f6",
        )
        param = Parameter(
            name="test",
            input_types=["str"],
            type="str",
            output_type="str",
            tooltip="test",
            badge=badge,
        )
        badge_result = param.get_badge()
        assert badge_result is not None
        assert badge_result.icon == "upload-cloud"
        assert badge_result.color == "#3b82f6"
        assert badge_result.to_dict()["icon"] == "upload-cloud"
        assert badge_result.to_dict()["color"] == "#3b82f6"

    def test_set_badge_with_color(self) -> None:
        """set_badge accepts color (hex, rgb, etc.)."""
        element = BaseNodeElement()
        element.set_badge(variant="info", color="#3b82f6")
        badge = element.get_badge()
        assert badge is not None
        assert badge.color == "#3b82f6"


class TestParameterGroup:
    def test_init(self) -> None:
        assert ParameterGroup(name="test")


class TestParameter:
    def test_init(self) -> None:
        assert Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")

    def test_on_incoming_connection_removed_initialized_empty(self) -> None:
        param = Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")
        assert param.on_incoming_connection_removed == []

    def test_on_outgoing_connection_removed_initialized_empty(self) -> None:
        param = Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")
        assert param.on_outgoing_connection_removed == []

    def test_on_incoming_connection_removed_stores_callbacks(self) -> None:
        param = Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")
        callback = lambda _p, _node_name, _param_name: None  # noqa: E731
        param.on_incoming_connection_removed.append(callback)
        assert callback in param.on_incoming_connection_removed

    def test_on_outgoing_connection_removed_stores_callbacks(self) -> None:
        param = Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")
        callback = lambda _p, _node_name, _param_name: None  # noqa: E731
        param.on_outgoing_connection_removed.append(callback)
        assert callback in param.on_outgoing_connection_removed

    def test_settable_property(self) -> None:
        """Test that settable property works correctly and is included in serialization."""
        # Test default value
        param = Parameter(name="test", input_types=["str"], type="str", output_type="str", tooltip="test")
        assert param.settable is True

        # Test setting to False
        param_false = Parameter(
            name="test", input_types=["str"], type="str", output_type="str", tooltip="test", settable=False
        )
        assert param_false.settable is False

        # Test property setter
        param.settable = False
        assert param.settable is False
        param.settable = True
        assert param.settable is True

        # Test serialization includes settable
        param_dict = param.to_dict()
        assert "settable" in param_dict
        assert param_dict["settable"] is True

        param_false_dict = param_false.to_dict()
        assert "settable" in param_false_dict
        assert param_false_dict["settable"] is False


def _make_param(name: str = "test_param", **kwargs) -> Parameter:
    """Helper to create a Parameter with minimal required args."""
    defaults = {"input_types": ["str"], "type": "str", "output_type": "str", "tooltip": "test"}
    defaults.update(kwargs)
    return Parameter(name=name, **defaults)


class TestParameterGroupParentSync:
    """Tests that ParameterGroup keeps parent_element_name in sync with parent_group_name."""

    def test_context_manager_sets_both_parent_fields(self) -> None:
        """Context manager creation sets both parent_group_name and parent_element_name."""
        with ParameterGroup(name="my_group"):
            param = _make_param()

        assert param.parent_group_name == "my_group"
        assert param.parent_element_name == "my_group"

    def test_explicit_add_child_sets_both_parent_fields(self) -> None:
        """Calling add_child() directly should set both parent fields."""
        group = ParameterGroup(name="my_group")
        param = _make_param()

        group.add_child(param)

        assert param.parent_group_name == "my_group"
        assert param.parent_element_name == "my_group"

    def test_add_child_non_parameter_does_not_set_parent_element_name(self) -> None:
        """Non-Parameter children get parent_group_name but not parent_element_name."""
        group = ParameterGroup(name="my_group")
        element = BaseNodeElement()

        group.add_child(element)

        assert element.parent_group_name == "my_group"
        assert not hasattr(element, "parent_element_name")

    def test_remove_child_by_reference_clears_both_fields(self) -> None:
        """Removing a Parameter by reference should clear both parent fields."""
        group = ParameterGroup(name="my_group")
        param = _make_param()
        group.add_child(param)

        group.remove_child(param)

        assert param.parent_group_name is None
        assert param.parent_element_name is None

    def test_remove_child_by_name_clears_both_fields(self) -> None:
        """Removing a Parameter by name (string) should clear both parent fields."""
        group = ParameterGroup(name="my_group")
        param = _make_param(name="removable")
        group.add_child(param)

        group.remove_child("removable")

        assert param.parent_group_name is None
        assert param.parent_element_name is None

    def test_remove_child_non_parameter_does_not_touch_parent_element_name(self) -> None:
        """Removing a non-Parameter child should clear parent_group_name only."""
        group = ParameterGroup(name="my_group")
        element = BaseNodeElement(element_id="child1")
        group.add_child(element)

        group.remove_child(element)

        assert element.parent_group_name is None
        assert not hasattr(element, "parent_element_name")

    def test_context_manager_overrides_explicit_none(self) -> None:
        """Context manager's add_child() overrides an explicit parent_element_name=None."""
        with ParameterGroup(name="my_group"):
            param = Parameter(
                name="test",
                input_types=["str"],
                type="str",
                output_type="str",
                tooltip="test",
                parent_element_name=None,  # explicit None, should be overridden
            )

        assert param.parent_element_name == "my_group"

    def test_serialization_includes_parent_element_name(self) -> None:
        """to_dict() should reflect the synced parent_element_name."""
        with ParameterGroup(name="my_group"):
            param = _make_param()

        param_dict = param.to_dict()
        assert param_dict["parent_element_name"] == "my_group"
        assert param_dict["parent_group_name"] == "my_group"


class TestParameterButtonGroupParentSync:
    """Same sync behavior for ParameterButtonGroup."""

    def test_context_manager_sets_both_parent_fields(self) -> None:
        with ParameterButtonGroup(name="btn_group"):
            param = _make_param()

        assert param.parent_group_name == "btn_group"
        assert param.parent_element_name == "btn_group"

    def test_explicit_add_child_sets_both_parent_fields(self) -> None:
        group = ParameterButtonGroup(name="btn_group")
        param = _make_param()

        group.add_child(param)

        assert param.parent_group_name == "btn_group"
        assert param.parent_element_name == "btn_group"

    def test_remove_child_by_reference_clears_both_fields(self) -> None:
        group = ParameterButtonGroup(name="btn_group")
        param = _make_param()
        group.add_child(param)

        group.remove_child(param)

        assert param.parent_group_name is None
        assert param.parent_element_name is None

    def test_remove_child_by_name_clears_both_fields(self) -> None:
        group = ParameterButtonGroup(name="btn_group")
        param = _make_param(name="removable")
        group.add_child(param)

        group.remove_child("removable")

        assert param.parent_group_name is None
        assert param.parent_element_name is None


class TestParameterConstructorOrdering:
    """Tests that Parameter.__init__ sets parent fields before super().__init__()."""

    def test_constructor_default_without_context(self) -> None:
        """Without a context manager, parent fields should be None (the default)."""
        param = _make_param()

        assert param.parent_container_name is None
        assert param.parent_element_name is None

    def test_constructor_explicit_values_without_context(self) -> None:
        """Explicit parent values should be preserved when no context manager is active."""
        param = _make_param(parent_container_name="container1", parent_element_name="group1")

        assert param.parent_container_name == "container1"
        assert param.parent_element_name == "group1"

    def test_context_manager_wins_over_explicit_none(self) -> None:
        """Context manager overrides constructor default, proving assignment is before super()."""
        with ParameterGroup(name="ctx_group"):
            param = _make_param(parent_element_name=None)

        assert param.parent_element_name == "ctx_group"

    def test_nested_groups_innermost_wins(self) -> None:
        """When groups are nested, the innermost (active) context should win."""
        with ParameterGroup(name="outer"), ParameterGroup(name="inner"):
            param = _make_param()

        assert param.parent_group_name == "inner"
        assert param.parent_element_name == "inner"


class TestAreTypesCompatible:
    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            # Exact matches.
            ("list", "list", True),
            ("list[str]", "list[str]", True),
            # A parameterized list satisfies a bare or `any` list target.
            ("list[str]", "list", True),
            ("list[str]", "list[any]", True),
            # A bare list does NOT satisfy a parameterized target. ParameterList opts into
            # accepting one separately, by advertising a bare `list` among its input_types.
            ("list", "list[str]", False),
            ("list[any]", "list[str]", False),
            # Mismatched element types never connect.
            ("list[str]", "list[int]", False),
            # Scalars and lists are not interchangeable.
            ("str", "list[str]", False),
            ("list[str]", "str", False),
        ],
    )
    def test_list_compatibility(self, source: str, target: str, expected: bool) -> None:  # noqa: FBT001
        assert ParameterType.are_types_compatible(source_type=source, target_type=target) is expected


class TestParameterListInputTypes:
    def test_advertises_parameterized_and_bare_list(self) -> None:
        """The container accepts `list[X]` for each declared type, plus an unparameterized `list`."""
        param_list = ParameterList(name="refs", tooltip="t", input_types=["ImageUrlArtifact", "str"])

        assert param_list.input_types == ["list[ImageUrlArtifact]", "list[str]", "list"]

    def test_accepts_list_sources_and_rejects_scalars(self) -> None:
        param_list = ParameterList(name="refs", tooltip="t", input_types=["ImageUrlArtifact"])

        # A list-producing node connects whether or not it declares an element type.
        assert param_list.is_incoming_type_allowed("list[ImageUrlArtifact]") is True
        assert param_list.is_incoming_type_allowed("list") is True
        assert param_list.is_incoming_type_allowed("list[any]") is True
        # A single artifact still belongs on a child row, not on the container.
        assert param_list.is_incoming_type_allowed("ImageUrlArtifact") is False
        assert param_list.is_incoming_type_allowed("str") is False

    def test_children_keep_the_declared_element_types(self) -> None:
        """Only the container widens; child rows still accept the bare element type."""
        param_list = ParameterList(name="refs", tooltip="t", input_types=["ImageUrlArtifact"])
        child = param_list.add_child_parameter()

        assert child.input_types == ["ImageUrlArtifact"]
        assert child.is_incoming_type_allowed("ImageUrlArtifact") is True
