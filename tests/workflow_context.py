"""Workflow registry helpers for tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine


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
    try:
        yield push_unsaved_workflow(engine)
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def push_unsaved_workflow(
    engine: Engine, display_name: str = "unsaved", *, working_directory: str | None = None
) -> Workflow:
    """Open a new unsaved workflow named `display_name` and return its registry entry."""
    engine.context_manager.push_workflow(workflow_name=display_name, working_directory=working_directory)
    return engine.context_manager.get_current_workflow()
