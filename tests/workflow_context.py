"""Workflow registry helpers for tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.app_events import OpenedWorkflowResult
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def bind(workflow: WorkflowRuntime) -> WorkflowRuntime:
    """Bind `workflow` for the rest of the test, as a client names its tab's workflow on every request.

    The `reset_workflow_binding` fixture drops the binding when the test ends.
    """
    bound_workflow.set(workflow)
    return workflow


def unbind() -> None:
    """Drop the test's binding, such as once the workflow it bound has closed."""
    bound_workflow.set(None)


def open_and_bind(
    engine: Engine,
    workflow_name: str | None = None,
    *,
    file_path: str | None = None,
    working_directory: str | None = None,
) -> WorkflowRuntime:
    """Open a workflow as `push_workflow` does, and bind it for the rest of the test."""
    _drop_closed_binding()
    return bind(
        engine.context_manager.open_workflow(workflow_name, file_path=file_path, working_directory=working_directory)
    )


def handle_and_bind(engine: Engine, request: RequestPayload) -> ResultPayload:
    """Send a request that opens a workflow, and bind the workflow it reports in `open_workflow` for the rest of the test.

    A binding to a workflow that has closed since is dropped first, so the request isn't refused for it.
    """
    _drop_closed_binding()
    result = engine.handle_request(request)
    if result.succeeded() and isinstance(result, OpenedWorkflowResult) and result.open_workflow is not None:
        bind(engine.open_workflows.get(result.open_workflow.workflow_id))
    return result


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
    Closes whatever was open before and after, and binds the workflow while it is open.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    token = bound_workflow.set(bound_workflow.get())
    try:
        yield push_unsaved_workflow(engine)
    finally:
        bound_workflow.reset(token)
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def push_unsaved_workflow(
    engine: Engine, display_name: str = "unsaved", *, working_directory: str | None = None
) -> Workflow:
    """Open a new unsaved workflow named `display_name`, bind it for the rest of the test, and return its registry entry."""
    open_and_bind(engine, display_name, working_directory=working_directory)
    return engine.context_manager.get_current_workflow()


def _drop_closed_binding() -> None:
    binding = bound_workflow.get()
    if binding is not None and binding is not NO_WORKFLOW and binding.closed:
        unbind()
