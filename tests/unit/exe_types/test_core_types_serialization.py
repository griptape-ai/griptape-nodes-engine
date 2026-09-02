"""Tests for BaseNodeElement/Parameter `to_dict()` serialization.

`to_dict()` is what the editor's element tree and node-creation replay commands are built
from, so its shape is a contract: every element type must produce a JSON-safe dict, group
membership must survive the round trip through the tree, and each Parameter subclass's
UI-only constructor sugar must land in `ui_options` where the frontend expects it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import (
    BadgeData,
    BaseNodeElement,
    DeprecationMessage,
    Parameter,
    ParameterGroup,
    ParameterMessage,
    ParameterMode,
)
from griptape_nodes.exe_types.param_types.parameter_audio import ParameterAudio
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_dict import ParameterDict
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_json import ParameterJson
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.exe_types.param_types.parameter_xml import ParameterXml
from griptape_nodes.exe_types.param_types.parameter_yaml import ParameterYaml
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.options import Options


class TestBaseNodeElementToDict:
    """`BaseNodeElement.to_dict()` produces a JSON-safe, order-preserving tree."""

    def test_to_dict_is_json_serializable(self) -> None:
        element = BaseNodeElement(name="root")
        child_a = BaseNodeElement(name="child_a")
        child_b = BaseNodeElement(name="child_b")
        element.add_child(child_a)
        element.add_child(child_b)

        # Must not raise: everything to_dict() produces has to cross the wire as JSON.
        json.dumps(element.to_dict())

    def test_children_serialize_recursively_in_order(self) -> None:
        # `to_dict()` on the base class does not surface `name` (that is added by
        # subclasses like Parameter), so order is asserted via `element_id`.
        parent = BaseNodeElement(name="parent", element_id="parent-id")
        first = BaseNodeElement(name="first", element_id="first-id")
        second = BaseNodeElement(name="second", element_id="second-id")
        third = BaseNodeElement(name="third", element_id="third-id")
        parent.add_child(first)
        parent.add_child(second)
        parent.add_child(third)

        child_ids = [child["element_id"] for child in parent.to_dict()["children"]]

        assert child_ids == ["first-id", "second-id", "third-id"]

    def test_grandchildren_are_present_in_nested_children(self) -> None:
        root = BaseNodeElement(name="root", element_id="root-id")
        mid = BaseNodeElement(name="mid", element_id="mid-id")
        leaf = BaseNodeElement(name="leaf", element_id="leaf-id")
        root.add_child(mid)
        mid.add_child(leaf)

        root_dict = root.to_dict()

        assert root_dict["children"][0]["element_id"] == "mid-id"
        assert root_dict["children"][0]["children"][0]["element_id"] == "leaf-id"

    def test_element_type_defaults_to_class_name(self) -> None:
        element = BaseNodeElement(name="root")

        assert element.to_dict()["element_type"] == "BaseNodeElement"

    def test_badge_set_appears_in_dict(self) -> None:
        element = BaseNodeElement(name="root")
        element.set_badge(variant="warning", title="Heads up", message="Something to check")

        badge_dict = element.to_dict()["badge"]

        assert badge_dict is not None
        assert badge_dict["variant"] == "warning"
        assert badge_dict["title"] == "Heads up"
        assert badge_dict["message"] == "Something to check"

    def test_badge_cleared_is_none(self) -> None:
        element = BaseNodeElement(name="root")
        element.set_badge(variant="info", message="temporary")
        element.clear_badge()

        assert element.to_dict()["badge"] is None

    def test_no_badge_set_is_none(self) -> None:
        element = BaseNodeElement(name="root")

        assert element.to_dict()["badge"] is None


class TestDeprecationMessageElementType:
    """`DeprecationMessage` must still report as `ParameterMessage` so the UI recognizes it."""

    def test_deprecation_message_reports_parameter_message_element_type(self) -> None:
        message = DeprecationMessage(
            value="This parameter is deprecated.",
            button_text="Migrate",
            migrate_function=lambda _old, _new: None,
        )

        assert message.to_dict()["element_type"] == "ParameterMessage"
        # Sanity: the live Python class is still DeprecationMessage; only the wire shape
        # is pinned to the parent's element_type.
        assert type(message).__name__ == "DeprecationMessage"

    def test_deprecation_message_to_dict_is_json_serializable(self) -> None:
        message = DeprecationMessage(
            value="This parameter is deprecated.",
            button_text="Migrate",
            migrate_function=lambda _old, _new: None,
        )

        json.dumps(message.to_dict())


class TestParameterGroupMembership:
    """A Parameter's group membership fields must stay in sync with the tree it lives in.

    `ParameterGroup.add_child` keeps `parent_group_name` (set for every element type) and
    `parent_element_name` (a Parameter-only field cattrs uses when replaying commands) in
    sync, per the comment on `ParameterGroup.add_child`. Both must point at the group while
    the Parameter is a member, and both must clear on removal so it reloads as a
    root-level Parameter instead of a member of a group that no longer claims it.
    """

    def test_parameter_added_to_group_has_group_name_in_both_fields(self) -> None:
        group = ParameterGroup(name="settings")
        param = Parameter(name="threshold", tooltip="t", default_value=0.5)
        group.add_child(param)

        param_dict = param.to_dict()

        assert param_dict["parent_group_name"] == "settings"
        assert param_dict["parent_element_name"] == "settings"

    def test_parameter_outside_a_group_has_no_group_reference(self) -> None:
        param = Parameter(name="threshold", tooltip="t", default_value=0.5)

        param_dict = param.to_dict()

        assert param_dict["parent_group_name"] is None
        assert param_dict["parent_element_name"] is None

    def test_removing_parameter_from_group_clears_both_fields(self) -> None:
        group = ParameterGroup(name="settings")
        param = Parameter(name="threshold", tooltip="t", default_value=0.5)
        group.add_child(param)
        group.remove_child(param)

        param_dict = param.to_dict()

        assert param_dict["parent_group_name"] is None
        assert param_dict["parent_element_name"] is None

    def test_group_to_dict_lists_its_parameters_as_children(self) -> None:
        group = ParameterGroup(name="settings")
        first = Parameter(name="threshold", tooltip="t", default_value=0.5)
        second = Parameter(name="mode", tooltip="t", default_value="auto")
        group.add_child(first)
        group.add_child(second)

        child_names = [child["name"] for child in group.to_dict()["children"]]

        assert child_names == ["threshold", "mode"]


class TestParameterToDict:
    """`Parameter.to_dict()` reports every field save/load and the editor rely on."""

    def test_to_dict_is_json_serializable(self) -> None:
        param = Parameter(
            name="config",
            tooltip="A config value",
            default_value={"nested": [1, 2, {"three": 3}]},
        )

        json.dumps(param.to_dict())

    @pytest.mark.parametrize(
        ("allowed_modes", "expected"),
        [
            ({ParameterMode.INPUT}, {"input": True, "property": False, "output": False}),
            ({ParameterMode.OUTPUT}, {"input": False, "property": False, "output": True}),
            (
                {ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                {"input": True, "property": True, "output": True},
            ),
        ],
    )
    def test_to_dict_reports_allowed_modes(self, allowed_modes: set[ParameterMode], expected: dict[str, bool]) -> None:
        param = Parameter(name="p", tooltip="t", allowed_modes=allowed_modes)

        param_dict = param.to_dict()

        assert param_dict["mode_allowed_input"] == expected["input"]
        assert param_dict["mode_allowed_property"] == expected["property"]
        assert param_dict["mode_allowed_output"] == expected["output"]

    def test_serializable_false_is_reported(self) -> None:
        param = Parameter(name="p", tooltip="t", serializable=False)

        assert param.to_dict()["serializable"] is False

    def test_serializable_true_is_reported_by_default(self) -> None:
        param = Parameter(name="p", tooltip="t")

        assert param.to_dict()["serializable"] is True

    def test_ui_options_includes_explicit_options(self) -> None:
        param = Parameter(name="p", tooltip="t", ui_options={"placeholder": "enter a value"})

        assert param.to_dict()["ui_options"]["placeholder"] == "enter a value"

    def test_parent_container_name_reported(self) -> None:
        param = Parameter(name="p", tooltip="t", parent_container_name="some_list")

        assert param.to_dict()["parent_container_name"] == "some_list"

    def test_tooltip_variants_reported(self) -> None:
        param = Parameter(
            name="p",
            tooltip="default tip",
            tooltip_as_input="input tip",
            tooltip_as_property="property tip",
            tooltip_as_output="output tip",
        )

        param_dict = param.to_dict()

        assert param_dict["tooltip"] == "default tip"
        assert param_dict["tooltip_as_input"] == "input tip"
        assert param_dict["tooltip_as_property"] == "property tip"
        assert param_dict["tooltip_as_output"] == "output tip"

    def test_is_user_defined_reported(self) -> None:
        param = Parameter(name="p", tooltip="t", user_defined=True)

        assert param.to_dict()["is_user_defined"] is True

    def test_default_value_reported(self) -> None:
        expected_default = 42
        param = Parameter(name="p", tooltip="t", default_value=expected_default)

        assert param.to_dict()["default_value"] == expected_default

    def test_type_input_types_and_output_type_reported(self) -> None:
        param = Parameter(name="p", tooltip="t", type="str", input_types=["str", "int"], output_type="str")

        param_dict = param.to_dict()

        assert param_dict["type"] == "str"
        assert param_dict["input_types"] == ["str", "int"]
        assert param_dict["output_type"] == "str"


class TestParameterMessageToDict:
    """`ParameterMessage.to_dict()` merges message styling into `ui_options`."""

    def test_message_ui_options_carries_variant_and_title(self) -> None:
        message = ParameterMessage(variant="warning", value="careful")

        message_dict = message.to_dict()

        assert message_dict["ui_options"]["variant"] == "warning"
        assert message_dict["ui_options"]["title"] == "Warning"

    def test_default_value_mirrors_value_for_backward_compatibility(self) -> None:
        message = ParameterMessage(variant="info", value="hello")

        message_dict = message.to_dict()

        assert message_dict["value"] == "hello"
        assert message_dict["default_value"] == "hello"

    def test_explicit_title_overrides_variant_default(self) -> None:
        message = ParameterMessage(variant="info", value="hello", title="Custom Title")

        assert message.to_dict()["ui_options"]["title"] == "Custom Title"

    def test_to_dict_is_json_serializable(self) -> None:
        message = ParameterMessage(variant="error", value="something broke")

        json.dumps(message.to_dict())


class TestTraitToDict:
    """`Trait.to_dict()` reports trait identity, and a trait's choices reach the parameter."""

    def test_trait_to_dict_includes_trait_name_and_ui_options(self) -> None:
        options = Options(choices=["a", "b", "c"])

        options_dict = options.to_dict()

        assert options_dict["trait_name"] == "Options"
        assert options_dict["trait_ui_options"]["simple_dropdown"] == ["a", "b", "c"]

    def test_options_trait_choices_appear_in_parameter_ui_options(self) -> None:
        param = Parameter(name="p", tooltip="t", traits={Options(choices=["red", "green", "blue"])})

        assert param.to_dict()["ui_options"]["simple_dropdown"] == ["red", "green", "blue"]

    def test_multi_options_trait_choices_appear_in_parameter_ui_options(self) -> None:
        param = Parameter(name="p", tooltip="t", traits={MultiOptions(choices=["x", "y", "z"])})

        multi_options = param.to_dict()["ui_options"]["multi_options"]

        assert multi_options["choices"] == ["x", "y", "z"]

    def test_explicit_ui_options_win_over_stale_trait_choices(self) -> None:
        """A saved parameter's own `ui_options` is the source of truth over a trait's default.

        Intended contract per the "SERIALIZATION BUG FIX" note on `Options`: after reload,
        a node rebuilds its trait with its ORIGINAL constructor choices (there is no
        mechanism to persist a runtime `trait.choices` mutation back into node source), so
        `Parameter._ui_options` -- populated from the saved `ui_options` during load -- must
        win the merge over the trait's now-stale default list.
        """
        param = Parameter(
            name="p",
            tooltip="t",
            traits={Options(choices=["stale default 1", "stale default 2"])},
            ui_options={"simple_dropdown": ["restored choice 1", "restored choice 2"]},
        )

        assert param.to_dict()["ui_options"]["simple_dropdown"] == ["restored choice 1", "restored choice 2"]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DATA-LOSS: Bug: Options.choices setter's 'dual sync' write of "
            "self._parent.ui_options['simple_dropdown'] = value is a no-op. Parameter.ui_options "
            "is a computed property that rebuilds a brand-new dict via `|` on every access "
            "(core_types.py ~:1878), so subscript-assigning into its return value never persists "
            "to Parameter._ui_options. A dynamic runtime update of trait.choices AFTER the trait "
            "is attached to a parameter should be reflected in that parameter's persisted "
            "ui_options (the whole point of the documented 'dual sync' fix), but it is not. "
            "- see #5440"
        ),
    )
    def test_dynamic_choices_update_after_attachment_persists_to_parameter_ui_options(self) -> None:
        options = Options(choices=["initial 1", "initial 2"])
        param = Parameter(name="p", tooltip="t", traits={options})

        options.choices = ["updated 1", "updated 2"]

        assert param._ui_options.get("simple_dropdown") == ["updated 1", "updated 2"]


class TestParamTypeSubclassUiOptions:
    """Each convenience Parameter subclass's constructor sugar lands in `ui_options`."""

    def test_parameter_bool_labels_appear_in_ui_options(self) -> None:
        param = ParameterBool(name="enabled", on_label="Yes", off_label="No")

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["on_label"] == "Yes"
        assert ui_options["off_label"] == "No"

    def test_parameter_bool_type_is_bool(self) -> None:
        param = ParameterBool(name="enabled", default_value=True)

        assert param.to_dict()["type"] == "bool"

    def test_parameter_dict_type_is_always_dict(self) -> None:
        param = ParameterDict(name="config", default_value={"a": 1})

        param_dict = param.to_dict()

        assert param_dict["type"] == "dict"
        assert param_dict["output_type"] == "dict"

    def test_parameter_int_step_appears_in_ui_options(self) -> None:
        expected_step = 5
        param = ParameterInt(name="count", step=expected_step, default_value=10)

        assert param.to_dict()["ui_options"]["step"] == expected_step

    def test_parameter_int_type_is_int(self) -> None:
        param = ParameterInt(name="count", default_value=10)

        assert param.to_dict()["type"] == "int"

    def test_parameter_image_ui_flags_appear_in_ui_options(self) -> None:
        param = ParameterImage(name="input_image", webcam_capture_image=True, edit_mask=True)

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["webcam_capture_image"] is True
        assert ui_options["edit_mask"] is True

    def test_parameter_video_ui_flags_appear_in_ui_options(self) -> None:
        param = ParameterVideo(name="input_video", webcam_capture_video=True, edit_video=True)

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["webcam_capture_video"] is True
        assert ui_options["edit_video"] is True

    def test_parameter_audio_ui_flags_appear_in_ui_options(self) -> None:
        param = ParameterAudio(name="input_audio", microphone_capture_audio=True, edit_audio=True)

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["microphone_capture_audio"] is True
        assert ui_options["edit_audio"] is True

    def test_parameter_xml_button_options_appear_in_ui_options(self) -> None:
        param = ParameterXml(name="doc", button=True, button_label="Format")

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["button"] is True
        assert ui_options["button_label"] == "Format"

    def test_parameter_yaml_button_options_appear_in_ui_options(self) -> None:
        param = ParameterYaml(name="doc", button=True, button_label="Validate")

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["button"] is True
        assert ui_options["button_label"] == "Validate"

    def test_parameter_json_button_options_appear_in_ui_options(self) -> None:
        param = ParameterJson(name="doc", button=True, button_label="Pretty print")

        ui_options = param.to_dict()["ui_options"]

        assert ui_options["button"] is True
        assert ui_options["button_label"] == "Pretty print"

    @pytest.mark.parametrize(
        "make_param",
        [
            lambda: ParameterBool(name="p", default_value=True),
            lambda: ParameterDict(name="p", default_value={}),
            lambda: ParameterInt(name="p", default_value=1),
            lambda: ParameterImage(name="p"),
            lambda: ParameterVideo(name="p"),
            lambda: ParameterAudio(name="p"),
            lambda: ParameterXml(name="p", default_value="<a/>"),
            lambda: ParameterYaml(name="p", default_value="a: 1"),
            lambda: ParameterJson(name="p", default_value={}),
        ],
    )
    def test_subclass_to_dict_is_json_serializable(self, make_param: Any) -> None:
        param = make_param()

        json.dumps(param.to_dict())


class TestBadgeDataToDict:
    """`BadgeData.to_dict()` is the shape both `BaseNodeElement.to_dict()` and events use."""

    def test_defaults_round_trip(self) -> None:
        badge = BadgeData()

        badge_dict = badge.to_dict()

        assert badge_dict["variant"] == "info"
        assert badge_dict["hide"] is False
        assert badge_dict["hide_clear_button"] is True

    def test_all_fields_reported(self) -> None:
        badge = BadgeData(
            variant="error",
            title="Oops",
            message="Something failed",
            icon="x-circle",
            color="#ff0000",
            hide=True,
            hide_clear_button=False,
        )

        badge_dict = badge.to_dict()

        assert badge_dict["variant"] == "error"
        assert badge_dict["title"] == "Oops"
        assert badge_dict["message"] == "Something failed"
        assert badge_dict["icon"] == "x-circle"
        assert badge_dict["color"] == "#ff0000"
        assert badge_dict["hide"] is True
        assert badge_dict["hide_clear_button"] is False

    def test_to_dict_does_not_leak_the_private_parent_reference(self) -> None:
        param = Parameter(name="p", tooltip="t")
        badge = BadgeData()
        badge._parent_element = param

        badge_dict = badge.to_dict()

        assert "_parent_element" not in badge_dict
        json.dumps(badge_dict)
