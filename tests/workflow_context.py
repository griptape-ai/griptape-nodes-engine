"""Open a workflow for tests that need one in context."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine


def push_unsaved_workflow(
    engine: Engine, display_name: str = "Untitled", working_directory: str | None = None
) -> Workflow:
    """Register a new unsaved workflow and make it the current context.

    The context holds only a registered workflow's id, so a test cannot push a bare name.
    """
    workflow = engine.workflow_registry.register_unsaved(display_name, working_directory)
    engine.context_manager.push_workflow(workflow_id=workflow.workflow_id)
    return workflow


@contextmanager
def empty_workflow_registry(engine: Engine) -> Iterator[None]:
    """Run with the engine's workflow registry empty, restoring what it held afterwards."""
    registry = engine.workflow_registry
    with (
        patch.dict(registry._by_id, {}, clear=True),
        patch.dict(registry._id_by_path, {}, clear=True),
        patch.dict(registry._id_by_name, {}, clear=True),
    ):
        yield


@contextmanager
def as_worker_process(engine: Engine) -> Iterator[None]:
    """Run with the engine's open workflow set aside, as a worker process, which opens none.

    For tests where one engine plays both roles: it opens a workflow to build nodes as the
    orchestrator, then runs one of them as a worker would, and a worker never has a workflow open.
    """
    with patch.dict(engine.open_workflows._by_id, {}, clear=True):
        yield


@contextmanager
def unsaved_workflow_open(engine: Engine) -> Iterator[Workflow]:
    """Run with an unsaved workflow open that names no folder, so `{workflow_dir}` answers the workspace root.

    For tests of path handlers that resolve the default project's `{workflow_dir?:/}` directories.
    Closes whatever was open before and after.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield push_unsaved_workflow(engine)
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
