"""Fixtures shared by every test suite."""

from collections.abc import Iterator

import pytest

from griptape_nodes.retained_mode.runtime.binding import bound_workflow


@pytest.fixture(autouse=True)
def reset_workflow_binding() -> Iterator[None]:
    """Start each test bound to no workflow, and drop a binding the test left (see `tests.workflow_context.bind`)."""
    token = bound_workflow.set(None)
    yield
    bound_workflow.reset(token)
