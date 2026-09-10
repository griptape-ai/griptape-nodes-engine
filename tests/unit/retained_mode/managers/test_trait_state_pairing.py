"""Saved trait entries pair with attached traits by class, not by name."""

import sys
from collections.abc import Generator
from types import ModuleType

import pytest

from griptape_nodes.exe_types.core_types import Parameter, Trait
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider


class Twin(Trait):
    """Stands in for a trait whose name another library also uses."""

    def __init__(self, *, tag: str = "local") -> None:
        super().__init__()
        self.tag = tag

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["twin"]


@pytest.fixture
def foreign_twin() -> Generator[ModuleType, None, None]:
    """A second Trait class named Twin, importable from its own module.

    Two libraries shipping a trait of the same name is the case a name-keyed lookup cannot
    tell apart, so the collision has to be built rather than described.
    """
    module = ModuleType("tests_foreign_twin_library")
    foreign = type("Twin", (Twin,), {"__module__": module.__name__})
    module.Twin = foreign  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    yield module
    del sys.modules[module.__name__]


class TestPairingByClass:
    def test_a_trait_is_paired_with_the_entry_that_names_its_class(self) -> None:
        parameter = Parameter(name="p", tooltip="t", traits={Options(choices=["a"]), Slider(min_val=0, max_val=1)})

        NodeManager._apply_trait_states(
            parameter,
            [
                {
                    "trait_name": "Slider",
                    "trait_module": "griptape_nodes.traits.slider",
                    "trait_state": {"min_val": 2, "max_val": 8},
                },
                {
                    "trait_name": "Options",
                    "trait_module": "griptape_nodes.traits.options",
                    "trait_state": {"choices": ["b"]},
                },
            ],
        )

        slider = parameter.find_elements_by_type(Slider)[0]
        assert (slider.min, slider.max) == (2, 8)
        assert parameter.find_elements_by_type(Options)[0].choices == ["b"]

    def test_a_same_named_trait_from_another_library_is_not_mistaken_for_it(self, foreign_twin: ModuleType) -> None:
        local = Twin(tag="local")
        parameter = Parameter(name="p", tooltip="t", traits={local})

        # The saved entry names the other library's Twin, so it describes a trait this
        # parameter does not carry. Matching on the name alone would overwrite the local one.
        NodeManager._apply_trait_states(
            parameter,
            [{"trait_name": "Twin", "trait_module": foreign_twin.__name__, "trait_state": {"tag": "foreign"}}],
        )

        assert local.tag == "local"
        attached = parameter.find_elements_by_type(Twin)
        assert len(attached) == 2  # noqa: PLR2004
        assert {type(trait).__module__ for trait in attached} == {Twin.__module__, foreign_twin.__name__}


class TestPairingIsOneToOne:
    """A parameter can carry two traits of one class, and each entry describes one of them."""

    def test_both_traits_of_a_class_get_their_own_state(self) -> None:
        first = Options(choices=["a"])
        second = Options(choices=["b"])
        parameter = Parameter(name="p", tooltip="t", traits={first, second})
        attached = parameter.find_elements_by_type(Options)

        NodeManager._apply_trait_states(
            parameter,
            [
                {
                    "trait_name": "Options",
                    "trait_module": "griptape_nodes.traits.options",
                    "trait_state": {"choices": ["first"]},
                },
                {
                    "trait_name": "Options",
                    "trait_module": "griptape_nodes.traits.options",
                    "trait_state": {"choices": ["second"]},
                },
            ],
        )

        assert [trait.choices for trait in attached] == [["first"], ["second"]]

    def test_no_extra_trait_is_built_for_the_second_entry(self) -> None:
        parameter = Parameter(name="p", tooltip="t", traits={Options(choices=["a"]), Options(choices=["b"])})

        NodeManager._apply_trait_states(
            parameter,
            [
                {"trait_name": "Options", "trait_module": "griptape_nodes.traits.options", "trait_state": {}},
                {"trait_name": "Options", "trait_module": "griptape_nodes.traits.options", "trait_state": {}},
            ],
        )

        assert len(parameter.find_elements_by_type(Options)) == 2  # noqa: PLR2004


class TestStateTheTraitCannotAccept:
    def test_an_existing_trait_keeps_what_init_built(self) -> None:
        slider = Slider(min_val=0, max_val=1)
        parameter = Parameter(name="p", tooltip="t", traits={slider})

        NodeManager._apply_trait_states(
            parameter,
            [{"trait_name": "Slider", "trait_module": "griptape_nodes.traits.slider", "trait_state": {"min_val": 2}}],
        )

        assert (slider.min, slider.max) == (0, 1)

    def test_a_warning_names_the_trait(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="p", tooltip="t", traits={Slider(min_val=0, max_val=1)})
        caplog.set_level("WARNING", logger="griptape_nodes")

        NodeManager._apply_trait_states(
            parameter,
            [{"trait_name": "Slider", "trait_module": "griptape_nodes.traits.slider", "trait_state": {"min_val": 2}}],
        )

        assert any("Slider" in record.getMessage() for record in caplog.records)
