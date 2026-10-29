"""Which open workflow the current work acts on.

Dispatch binds the workflow a request's envelope names in a ContextVar, so its handler and the
tasks it spawns act on that workflow (see `Engine.bind_workflow` and `Engine.current_workflow`).
Kept apart from `WorkflowRuntime` so code can read the binding without importing what a workflow
holds.
"""

from __future__ import annotations

from contextvars import ContextVar, copy_context
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Coroutine

    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


class NoWorkflow(Enum):
    """Bound by work that explicitly acts on no workflow."""

    NO_WORKFLOW = "no_workflow"


NO_WORKFLOW: Final = NoWorkflow.NO_WORKFLOW

# None means nothing was bound, which is different from binding NO_WORKFLOW: unbound work acts on
# the only open workflow (see `Engine.current_workflow`), and NO_WORKFLOW work acts on none.
bound_workflow: ContextVar[WorkflowRuntime | NoWorkflow | None] = ContextVar(
    "griptape_nodes_bound_workflow", default=None
)


class WorkflowNotNamedError(Exception):
    """Work that needs a workflow named none, and several are open, so none can be chosen for it."""


class WorkflowNotOpenError(Exception):
    """Work bound to a workflow that has closed since, so it has no workflow to act on."""


def create_task_bound_to_no_workflow(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Start `coro` on `loop` as a task that acts on no workflow, for engine work that outlives the request starting it.

    A task copies the context it starts in, so it would otherwise keep acting on that request's
    workflow after the request ended, and after the workflow closed.
    """
    context = copy_context()
    context.run(bound_workflow.set, NO_WORKFLOW)
    return loop.create_task(coro, context=context)
