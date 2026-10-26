"""Regression guard: which wires a parameter that has not opted in to anything new accepts.

Type-adaptive pins and implicit conversion are opt-in. These tables pin the connection rules every
other parameter has always had, so neither feature can widen or narrow them by accident.
"""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterType

# (source type, target type, accepted)
_COMPATIBILITY_TABLE = [
    # Exact matches, case-insensitive.
    ("str", "str", True),
    ("int", "int", True),
    ("float", "float", True),
    ("bool", "bool", True),
    ("ImageArtifact", "ImageArtifact", True),
    ("ImageArtifact", "imageartifact", True),
    ("dict", "dict", True),
    ("json", "json", True),
    # No implicit conversions between scalar types.
    ("int", "str", False),
    ("float", "str", False),
    ("bool", "str", False),
    ("str", "int", False),
    ("str", "float", False),
    ("int", "float", False),
    ("float", "int", False),
    ("bool", "int", False),
    ("int", "bool", False),
    # No conversions between containers and JSON or text.
    ("dict", "json", False),
    ("json", "dict", False),
    ("list", "json", False),
    ("dict", "str", False),
    ("list", "str", False),
    ("str", "dict", False),
    ("str", "list", False),
    ("dict", "list", False),
    # An `any` target takes everything except `none`.
    ("str", "any", True),
    ("int", "any", True),
    ("ImageArtifact", "any", True),
    ("parametercontroltype", "any", True),
    ("any", "any", True),
    ("none", "any", False),
    # An `any` source fits only an `any` target.
    ("any", "str", False),
    ("any", "int", False),
    ("any", "ImageArtifact", False),
    # `none` never connects.
    ("none", "none", False),
    ("str", "none", False),
    # Generics flow to their bare base and to base[any], never the other way.
    ("list[str]", "list", True),
    ("list[any]", "list", True),
    ("list[str]", "list[any]", True),
    ("list[str]", "list[str]", True),
    ("list", "list[str]", False),
    ("list[any]", "list[str]", False),
    ("list[int]", "list[str]", False),
    ("dict[str, int]", "dict", True),
    ("list[str]", "dict", False),
    # Unrelated artifacts.
    ("ImageArtifact", "VideoArtifact", False),
    ("ImageUrlArtifact", "ImageArtifact", False),
]


class TestPlainParameterCompatibility:
    """The connection rules of parameters that opt in to nothing."""

    @pytest.mark.parametrize(("source_type", "target_type", "accepted"), _COMPATIBILITY_TABLE)
    def test_are_types_compatible_table(self, source_type: str, target_type: str, *, accepted: bool) -> None:
        assert ParameterType.are_types_compatible(source_type=source_type, target_type=target_type) is accepted

    @pytest.mark.parametrize(("source_type", "target_type", "accepted"), _COMPATIBILITY_TABLE)
    def test_plain_parameter_accepts_by_the_table(self, source_type: str, target_type: str, *, accepted: bool) -> None:
        target = Parameter(name="target", tooltip="t", type=target_type)

        assert target.is_incoming_type_allowed(source_type) is accepted

    def test_none_types_never_match(self) -> None:
        assert ParameterType.are_types_compatible(source_type=None, target_type="str") is False
        assert ParameterType.are_types_compatible(source_type="str", target_type=None) is False

    def test_all_source_reaches_every_parameter(self) -> None:
        target = Parameter(name="target", tooltip="t", type="int")

        assert target.is_incoming_type_allowed("all") is True

    def test_parameter_with_several_input_types_accepts_any_of_them(self) -> None:
        target = Parameter(name="target", tooltip="t", input_types=["int", "float"], type="int")

        assert target.is_incoming_type_allowed("float") is True
        assert target.is_incoming_type_allowed("str") is False

    def test_plain_parameter_is_not_adaptive(self) -> None:
        parameter = Parameter(name="p", tooltip="t", type="any")

        assert parameter.adaptive_type_group is None
        assert parameter.adaptive_resolved_type is None
