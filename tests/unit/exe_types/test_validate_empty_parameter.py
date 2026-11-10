"""`validate_empty_parameter` reports a blank text parameter.

`skip_if_connected=True` is for `validate_before_workflow_run`, which runs before connections deliver,
so a connected parameter is not blank there. Without it, which is how `process()` calls the helper
after the connection has delivered, an empty value is still an error.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode

from .mocks import MockNode


@pytest.fixture
def node() -> MockNode:
    """A node with one text parameter, `prompt`, that starts blank."""
    node = MockNode()
    node.add_parameter(
        Parameter(
            name="prompt",
            tooltip="t",
            type="str",
            default_value="",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        )
    )
    return node


@pytest.fixture
def connected() -> Iterator[None]:
    """Report every parameter on the node as having an incoming connection."""
    with patch.object(MockNode, "_param_has_incoming_connection", return_value=True):
        yield


@pytest.fixture
def disconnected() -> Iterator[None]:
    """Report every parameter on the node as having no incoming connection."""
    with patch.object(MockNode, "_param_has_incoming_connection", return_value=False):
        yield


@pytest.mark.usefixtures("disconnected")
def test_blank_unconnected_parameter_is_reported(node: MockNode) -> None:
    """Nothing typed and nothing wired: the parameter is blank."""
    error = node.validate_empty_parameter("prompt")
    assert isinstance(error, ValueError)
    assert 'Parameter "prompt" was left blank' in str(error)


@pytest.mark.usefixtures("disconnected")
def test_filled_parameter_is_not_reported(node: MockNode) -> None:
    """A typed value is not blank."""
    node.set_parameter_value("prompt", "a cat")
    assert node.validate_empty_parameter("prompt") is None


@pytest.mark.usefixtures("connected")
def test_blank_connected_parameter_is_still_reported_at_run_time(node: MockNode) -> None:
    """After the connection has delivered, an empty value is a real error, as `process()` relies on."""
    error = node.validate_empty_parameter("prompt")
    assert isinstance(error, ValueError)


@pytest.mark.usefixtures("connected")
def test_blank_connected_parameter_is_skipped_before_the_run(node: MockNode) -> None:
    """Before the run, a connected parameter counts as provided when asked to skip it."""
    assert node.validate_empty_parameter("prompt", skip_if_connected=True) is None


@pytest.mark.usefixtures("disconnected")
def test_skip_if_connected_does_not_excuse_an_unconnected_blank(node: MockNode) -> None:
    """The option only applies to connected parameters; an unwired blank is still reported."""
    error = node.validate_empty_parameter("prompt", skip_if_connected=True)
    assert isinstance(error, ValueError)
