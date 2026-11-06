"""Send a file handle's requests through the workflow it was built for.

A file handle resolves project macros like ``{outputs}`` against a workflow. A handle built where a
workflow is bound keeps it, so it acts on that workflow from any thread, including a plain
``threading.Thread`` or a thread pool, which do not carry the caller's binding.

A handle built where none is bound keeps None and sends each request as the code calling it is
bound. That is deliberate, not a fallback to remove: such a handle may be built before its node
joins a workflow and used later in ``aprocess()``, or read a plain path on the host, where no
workflow applies. Pinning None to "no workflow" would send those to the wrong place silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def workflow_building_now() -> WorkflowRuntime | None:
    """The workflow the calling code is bound to, which a handle built now keeps, or None."""
    binding = bound_workflow.get()
    if binding is None or binding is NO_WORKFLOW:
        return None
    return binding


def handle_request_in(workflow: WorkflowRuntime | None, request: RequestPayload) -> ResultPayload:
    """Send `request` through `workflow`, or, for a handle that kept none, as the calling code is bound."""
    if workflow is None:
        return GriptapeNodes.handle_request(request)
    return workflow.handle_request(request)


async def ahandle_request_in(workflow: WorkflowRuntime | None, request: RequestPayload) -> ResultPayload:
    """Like `handle_request_in`, awaiting the request."""
    if workflow is None:
        return await GriptapeNodes.ahandle_request(request)
    return await workflow.ahandle_request(request)
