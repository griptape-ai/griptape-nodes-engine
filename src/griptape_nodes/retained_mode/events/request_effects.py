"""What a request can do to a workflow, known before it runs, from the results it answers with."""

from __future__ import annotations

import functools

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    WorkflowAlteredMixin,
    WorkflowAlteredReportedMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@functools.cache
def never_alters_workflow(request_type: type[RequestPayload]) -> bool:
    """Whether a request of ``request_type`` never changes a workflow, such as one that only reads it.

    True when its success result extends `WorkflowNotAlteredMixin` and its failure result, if it has
    one, can't report a change. A request's results are found by name: ``<Name>Request`` answers with
    ``<Name>ResultSuccess`` and ``<Name>ResultFailure``. A request without a success result of that
    name may change the workflow.
    """
    name = request_type.__name__.removesuffix("Request")
    success = PayloadRegistry.get_type(f"{name}ResultSuccess")
    failure = PayloadRegistry.get_type(f"{name}ResultFailure")
    if success is None or not issubclass(success, WorkflowNotAlteredMixin):
        return False
    return failure is None or not issubclass(failure, (WorkflowAlteredMixin, WorkflowAlteredReportedMixin))
