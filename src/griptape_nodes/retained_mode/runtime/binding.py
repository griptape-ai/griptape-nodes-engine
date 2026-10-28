"""Which open workflow the current work acts on.

Dispatch binds the workflow a request's envelope names in a ContextVar, so its handler and the
tasks it spawns act on that workflow (see `Engine.bind_workflow` and `Engine.current_workflow`).
Kept apart from `WorkflowRuntime` so code can read the binding without importing what a workflow
holds.
"""

from __future__ import annotations

from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
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
