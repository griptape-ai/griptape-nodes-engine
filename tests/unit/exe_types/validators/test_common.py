"""Tests for `MaxCount` and `DependsOn` on their own."""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.exe_types.validators.common import DependsOn, MaxCount
from griptape_nodes.exe_types.validators.media import MaxFileSize, SideRange
from tests.unit.exe_types.mocks import MockNode

DOCS = "https://example.com/limits"


class TestMaxCount:
    def test_allows_the_maximum(self) -> None:
        assert MaxCount(3).check([1, 2, 3], subject="images", docs_url=None) is None

    def test_refuses_more_than_the_maximum(self) -> None:
        error = MaxCount(2, noun="images").check([1, 2, 3], subject="reference images", docs_url=DOCS)
        assert str(error) == "3 images are connected to 'reference images' and the limit is 2. Remove 1."
        assert error is not None
        assert error.links
        assert error.links[0].url == DOCS


class TestDependsOn:
    PRO = (SideRange(min=300),)
    DEFAULT = (SideRange(min=1000), MaxFileSize(1))

    def _node(self, model: str | None) -> MockNode:
        node = MockNode(name="node")
        node.add_parameter(ParameterString(name="model"))
        if model is not None:
            node.set_parameter_value("model", model)
        return node

    def test_picks_the_list_for_the_current_value(self) -> None:
        depends_on_model = DependsOn("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(depends_on_model.selected(self._node("pro"))) == list(self.PRO)

    @pytest.mark.parametrize("model", ["other", None])
    def test_falls_back_to_the_default(self, model: str | None) -> None:
        depends_on_model = DependsOn("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(depends_on_model.selected(self._node(model))) == list(self.DEFAULT)

    def test_has_no_default_unless_given(self) -> None:
        assert list(DependsOn("model", {"pro": self.PRO}).selected(self._node("other"))) == []

    def test_lists_every_validator_it_can_pick(self) -> None:
        depends_on_model = DependsOn("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(depends_on_model.candidates()) == [*self.PRO, *self.DEFAULT]

    def test_a_nested_depends_on_picks_in_turn(self) -> None:
        node = self._node("pro")
        node.add_parameter(ParameterString(name="size"))
        node.set_parameter_value("size", "large")
        nested = DependsOn(
            "model",
            {"pro": [MaxFileSize(1), DependsOn("size", {"large": self.PRO}, default=self.DEFAULT)]},
        )
        assert list(nested.selected(node)) == [MaxFileSize(1), *self.PRO]
        node.set_parameter_value("size", "small")
        assert list(nested.selected(node)) == [MaxFileSize(1), *self.DEFAULT]

    def test_lists_every_validator_reachable_through_nesting(self) -> None:
        nested = DependsOn("model", {"pro": [DependsOn("size", {"large": self.PRO})]}, default=self.DEFAULT)
        assert list(nested.candidates()) == [*self.PRO, *self.DEFAULT]

    @pytest.mark.parametrize(("upscale", "expected"), [(True, []), (False, [SideRange(min=512)])])
    def test_picks_by_a_value_that_is_not_text(self, upscale: bool, expected: list) -> None:  # noqa: FBT001
        node = MockNode(name="node")
        node.add_parameter(ParameterBool(name="upscale"))
        node.set_parameter_value("upscale", upscale)
        depends_on_upscale = DependsOn("upscale", {True: []}, default=[SideRange(min=512)])
        assert list(depends_on_upscale.selected(node)) == expected

    @pytest.mark.parametrize(("images", "expected"), [([], list(PRO)), (["a.png"], list(DEFAULT))])
    def test_key_turns_the_value_into_what_is_looked_up(self, images: list[str], expected: list) -> None:
        node = MockNode(name="node")
        node.add_parameter(
            ParameterList(
                name="reference_images",
                tooltip="",
                input_types=["ImageUrlArtifact"],
                type="ImageUrlArtifact",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        node.set_parameter_value("reference_images", images)
        depends_on_images = DependsOn("reference_images", {False: self.PRO}, default=self.DEFAULT, key=bool)
        assert list(depends_on_images.selected(node)) == expected

    def test_checks_nothing_itself(self) -> None:
        depends_on_model = DependsOn("model", {"pro": self.PRO})
        assert depends_on_model.check("x", subject="s", docs_url=None) is None

    def test_is_frozen_and_hashable(self) -> None:
        first = DependsOn("model", {"pro": self.PRO}, default=self.DEFAULT)
        second = DependsOn("model", {"pro": list(self.PRO)}, default=list(self.DEFAULT))
        assert first == second
        assert hash(first) == hash(second)
        with pytest.raises(AttributeError):
            first.parameter = "size"  # pyright: ignore[reportAttributeAccessIssue]
