"""Fixtures shared by every test suite."""

from collections.abc import Iterator

import pytest

from griptape_nodes.retained_mode.runtime.binding import bound_workflow, opened_binding


@pytest.fixture(autouse=True)
def reset_workflow_binding() -> Iterator[None]:
    """Start each test bound to no workflow, and drop a binding the test left.

    `ContextManager.push_workflow` binds the workflow it opens for the rest of the current work,
    and a test is one piece of work, so what it binds must not reach the next test.
    """
    bound_token = bound_workflow.set(None)
    opened_token = opened_binding.set(None)
    yield
    opened_binding.reset(opened_token)
    bound_workflow.reset(bound_token)
