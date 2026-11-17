"""A hook for watching reads of the current workflow that find none.

Nothing is installed in production. The unit tests install one to fail a HOST handler that depends on
a workflow it doesn't take (see `RequestScope`): such a handler finds no workflow even with one open.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload
    from griptape_nodes.retained_mode.request_handlers import RequestScope
    from griptape_nodes.retained_mode.runtime.binding import NoWorkflow
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


class WorkflowReadObserver(Protocol):
    def dispatching(
        self,
        request_type: type[RequestPayload],
        scope: RequestScope | None,
        binding: WorkflowRuntime | NoWorkflow | None,
    ) -> AbstractContextManager[object]:
        """Entered around a request's handler, once its binding is set."""
        ...

    def read_without_workflow(self, engine: Engine, read: str) -> None:
        """Called when work bound to no workflow reads the current workflow, describing the read."""
        ...


_observer: WorkflowReadObserver | None = None


def install_workflow_read_observer(observer: WorkflowReadObserver | None) -> WorkflowReadObserver | None:
    """Install `observer`, or none, and return the one it replaces."""
    global _observer  # noqa: PLW0603
    previous = _observer
    _observer = observer
    return previous


def observe_dispatch(
    request_type: type[RequestPayload],
    scope: RequestScope | None,
    binding: WorkflowRuntime | NoWorkflow | None,
) -> AbstractContextManager[object]:
    """The installed observer's context for a request's handler, or one that does nothing."""
    if _observer is None:
        return nullcontext()
    return _observer.dispatching(request_type, scope, binding)


def observe_read_without_workflow(engine: Engine, read: str) -> None:
    """Tell the installed observer, if any, that work bound to no workflow read the current workflow."""
    if _observer is None:
        return
    _observer.read_without_workflow(engine, read)
