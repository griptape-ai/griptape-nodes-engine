"""Tests for a HuggingFace component built over parameters a saved workflow already recreated.

Loading recreates parameters added after the node was built from saved data, which carries no
functions. The diffusers PipelineBuilder builds a fresh component on load instead of adding the
parameters again, so the component has to attach its handlers to the parameters it finds.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.retained_mode.events.model_events import ListModelDownloadsRequest, ListModelDownloadsResultSuccess
from griptape_nodes.traits.button import Button
from tests.unit.exe_types.mocks import MockNode

REPO = "black-forest-labs/FLUX.1-schnell"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    def handle_request(request: object) -> object:
        if isinstance(request, ListModelDownloadsRequest):
            return ListModelDownloadsResultSuccess(downloads=[], result_details="ok")
        msg = f"unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    module = "griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter"
    with (
        patch(f"{module}.list_repo_revisions_in_cache", return_value=[]),
        patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle_request),
    ):
        yield


def _button(parameter: Parameter | None) -> Button:
    assert parameter is not None
    (button,) = parameter.find_elements_by_type(Button)
    return button


def _strip_handlers_as_a_load_does(node: MockNode) -> None:
    for name in ("model", "model_download"):
        parameter = node.get_parameter_by_name(name)
        assert parameter is not None
        _button(parameter).on_click_callback = None
        parameter._converters = []


def test_a_component_built_after_load_attaches_its_handlers() -> None:
    """Both buttons and the converter get the new component's handlers back."""
    node = MockNode()
    HuggingFaceRepoParameter(node, repo_ids=[REPO], gated=False).add_input_parameters()
    _strip_handlers_as_a_load_does(node)

    component = HuggingFaceRepoParameter(node, repo_ids=[REPO], gated=False)

    model = node.get_parameter_by_name("model")
    assert model is not None
    assert _button(model).on_click_callback == component._on_refresh_click
    assert _button(node.get_parameter_by_name("model_download")).on_click_callback == component._on_download_click
    assert model._converters == [component._on_selection_changed]


def test_the_newest_component_replaces_an_earlier_ones_handlers() -> None:
    """PipelineBuilder builds a component for its default pipeline type first; the later one must win."""
    node = MockNode()
    HuggingFaceRepoParameter(node, repo_ids=[REPO], gated=False).add_input_parameters()

    component = HuggingFaceRepoParameter(node, repo_ids=[REPO], gated=False)

    model = node.get_parameter_by_name("model")
    assert model is not None
    assert model._converters == [component._on_selection_changed]
    assert _button(node.get_parameter_by_name("model_download")).on_click_callback == component._on_download_click
