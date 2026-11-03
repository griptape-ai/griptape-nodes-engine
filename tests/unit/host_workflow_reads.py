"""Records reads of the current workflow made for a HOST request a client sent, while a workflow is open.

Such a request acts on no workflow, so the read finds none, and its handler most likely depends on a
workflow it should take (see `RequestScope`). The autouse `fail_on_host_workflow_reads` fixture installs
a `HostWorkflowReadRecorder` and fails a test that makes one.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.request_handlers import RequestScope
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, NoWorkflow
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload

# The HOST request a client sent, while it and the work it issues are handled. None outside one, and
# inside any request bound to a workflow.
_host_request_from_client: ContextVar[type[RequestPayload] | None] = ContextVar(
    "tests_host_request_from_client", default=None
)


@dataclass(frozen=True)
class HostWorkflowRead:
    request_type_name: str
    read: str


@dataclass
class HostWorkflowReadRecorder:
    """A `WorkflowReadObserver` that records HOST workflow reads."""

    reads: list[HostWorkflowRead] = field(default_factory=list)

    @contextmanager
    def dispatching(
        self,
        request_type: type[RequestPayload],
        scope: RequestScope | None,
        binding: WorkflowRuntime | NoWorkflow | None,
    ) -> Iterator[None]:
        token = _host_request_from_client.set(self._host_request_for(request_type, scope, binding))
        try:
            yield
        finally:
            _host_request_from_client.reset(token)

    def read_without_workflow(self, engine: Engine, read: str) -> None:
        request_type = _host_request_from_client.get()
        if request_type is None or not engine.open_workflows.all_open():
            return
        self.reads.append(HostWorkflowRead(request_type_name=request_type.__name__, read=read))

    @staticmethod
    def _host_request_for(
        request_type: type[RequestPayload], scope: RequestScope | None, binding: WorkflowRuntime | NoWorkflow | None
    ) -> type[RequestPayload] | None:
        """The HOST request a client sent that this dispatch is for, if any.

        A HOST request is bound to no workflow. A request bound to a workflow is for that workflow. Any
        other request keeps what the work that issued it was for: a nested request that gets no workflow
        because a HOST request issued it is still for that HOST request.
        """
        if isinstance(binding, WorkflowRuntime):
            return None
        if scope is RequestScope.HOST and binding is NO_WORKFLOW:
            return request_type
        return _host_request_from_client.get()
