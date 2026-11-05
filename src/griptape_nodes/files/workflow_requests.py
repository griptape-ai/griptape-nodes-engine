"""Send a file handle's requests through the workflow it was built for.

A file handle resolves project macros like ``{outputs}`` against a workflow. It keeps the workflow
it was built for, so it acts on that workflow from any thread, including a plain
``threading.Thread`` or a thread pool, which do not carry the caller's binding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def workflow_building_now() -> WorkflowRuntime | None:
    """The workflow the calling code is bound to, which a handle built now acts on, or None."""
    binding = bound_workflow.get()
    if binding is None or binding is NO_WORKFLOW:
        return None
    return binding


def handle_request_in(workflow: WorkflowRuntime | None, request: RequestPayload) -> ResultPayload:
    """Send `request` through `workflow`, or, for a handle built for none, as the calling code is bound."""
    if workflow is None:
        return GriptapeNodes.handle_request(request)
    return workflow.handle_request(request)


async def ahandle_request_in(workflow: WorkflowRuntime | None, request: RequestPayload) -> ResultPayload:
    """Like `handle_request_in`, awaiting the request."""
    if workflow is None:
        return await GriptapeNodes.ahandle_request(request)
    return await workflow.ahandle_request(request)
