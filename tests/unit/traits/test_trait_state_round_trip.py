from typing import Any

import pytest

from griptape_nodes.traits.button import Button
from griptape_nodes.traits.clamp import Clamp
from griptape_nodes.traits.color_picker import ColorPicker
from griptape_nodes.traits.file_system_picker import FileSystemPicker
from griptape_nodes.traits.minmax import MinMax
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.numbers_selector import NumbersSelector
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.widget import Widget


@pytest.mark.parametrize(
    ("trait", "state"),
    [
        (Slider(min_val=1, max_val=9), {"min_val": 1, "max_val": 9, "soft_limits": False}),
        (Clamp(min_val=1, max_val=9), {"min_val": 1, "max_val": 9}),
        (MinMax(min_val=1, max_val=9), {"min_val": 1, "max_val": 9}),
        (ColorPicker(format="rgb"), {"format": "rgb"}),
        (
            Options(choices=["a"], show_search=False, search_filter="x", allow_custom=True),
            {"choices": ["a"], "show_search": False, "search_filter": "x", "allow_custom": True},
        ),
        (
            MultiOptions(choices=["a"], placeholder="Pick", icon_size="large"),
            {
                "choices": ["a"],
                "placeholder": "Pick",
                "max_selected_display": 3,
                "show_search": True,
                "search_filter": "",
                "icon_size": "large",
                "allow_user_created_options": False,
            },
        ),
        (
            NumbersSelector({"x": 1}, step=2, overall_min=0, overall_max=10),
            {"defaults": {"x": 1}, "step": 2, "overall_min": 0, "overall_max": 10},
        ),
        (Widget("editor", "widgets"), {"name": "editor", "library": "widgets"}),
    ],
)
def test_trait_reports_plain_constructor_state(trait: Any, state: dict[str, Any]) -> None:
    """Each built-in returns the state its existing constructor accepts."""
    assert trait.to_state() == state


def test_file_picker_reports_all_constructor_state() -> None:
    """A larger trait includes both list and scalar arguments."""
    trait = FileSystemPicker(file_extensions=[".png"], allow_files=True)

    state = trait.to_state()

    assert state["file_extensions"] == [".png"]
    assert state["allow_files"] is True


def test_button_omits_callbacks_from_state() -> None:
    """Node behavior is rebuilt by node code rather than saved as data."""

    def callback(_button: Button, _payload: Any) -> None:
        return None

    trait = Button(label="Run", on_click=callback)

    state = trait.to_state()

    assert state["label"] == "Run"
    assert "on_click" not in state
    assert "get_button_state" not in state


def test_apply_state_keeps_values_an_older_save_omits() -> None:
    """Missing keys preserve values supplied by current node code."""
    trait = MultiOptions(choices=["a"], placeholder="Pick")

    trait.apply_state({"choices": ["b"]})

    assert trait.choices == ["b"]
    assert trait.placeholder == "Pick"


def test_apply_state_uses_trait_validation() -> None:
    """Restore follows the same normalization as the constructor."""
    trait = MultiOptions(choices=["a"], icon_size="large")

    trait.apply_state({"icon_size": "unknown"})

    assert trait.icon_size == "small"
