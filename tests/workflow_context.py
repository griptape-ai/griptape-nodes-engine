"""Workflow registry helpers for tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.runtime.binding import bound_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@contextmanager
def empty_workflow_registry(engine: Engine) -> Iterator[None]:
    """Run with the engine's workflow registry empty, restoring what it held afterwards."""
    registry = engine.workflow_registry
    with patch.dict(registry._by_id, {}, clear=True):
        yield


@contextmanager
def unsaved_workflow_open(engine: Engine) -> Iterator[Workflow]:
    """Run with an unsaved workflow open that names no folder, so `{workflow_dir}` answers the workspace root.

    For tests of path handlers that resolve the default project's `{workflow_dir?:/}` directories.
    Closes whatever was open before and after.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    entry = push_unsaved_workflow(engine)
    workflow = engine.current_workflow()
    try:
        yield entry
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        unbind(workflow)


def push_unsaved_workflow(
    engine: Engine, display_name: str = "unsaved", *, working_directory: str | None = None
) -> Workflow:
    """Open a new unsaved workflow named `display_name` and return its registry entry."""
    engine.context_manager.push_workflow(workflow_name=display_name, working_directory=working_directory)
    return engine.context_manager.get_current_workflow()


def the_open_workflow(engine: Engine) -> WorkflowRuntime:
    """The one open workflow, for a test that works in it the way a client names it on every request."""
    open_workflows = engine.open_workflows.all_open()
    assert len(open_workflows) == 1, f"Expected exactly one open workflow, found {len(open_workflows)}."
    return open_workflows[0]


def send_in_the_open_workflow(engine: Engine, request: RequestPayload) -> ResultPayload:
    """Send `request` naming the one open workflow, as a client does with `open_workflow_id`."""
    return the_open_workflow(engine).handle_request(request)


async def send_in_the_open_workflow_async(engine: Engine, request: RequestPayload) -> ResultPayload:
    """Send `request` naming the one open workflow, as a client does with `open_workflow_id`."""
    return await the_open_workflow(engine).ahandle_request(request)


def clear_all_object_state(engine: Engine) -> None:
    """Close every open workflow with `ClearAllObjectStateRequest`, and end the bindings the test made to them."""
    opened = engine.open_workflows.all_open()
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    for workflow in opened:
        unbind(workflow)


def bind(workflow: WorkflowRuntime) -> None:
    """Bind `workflow` for the rest of the test, as `push_workflow` binds what it opens. The autouse fixture clears it."""
    bound_workflow.set(workflow)


def unbind(workflow: WorkflowRuntime) -> None:
    """End a `bind` to `workflow`, leaving a binding to any other workflow alone."""
    if bound_workflow.get() is workflow:
        bound_workflow.set(None)
