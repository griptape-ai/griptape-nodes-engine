"""Undo and redo, by checkpointing each workflow the way a save writes it and rebuilding from a checkpoint.

A step is the changes made by requests that share an `undo_group`. The checkpoint at the end of a
step is taken when the next step starts, or when an undo or redo needs it, so a step of many requests
costs one serialization. An undo deletes the workflow's flow and rebuilds it from the checkpoint before,
the same way opening a saved workflow builds it, so every node and connection comes back through the
code that makes them, with no hooks running twice.
"""

from __future__ import annotations

import json
import logging
import re
import zlib
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionPayload,
    ResultDetails,
    ResultPayload,
    RunControlMixin,
)
from griptape_nodes.retained_mode.events.flow_events import (
    DeleteFlowRequest,
    DeserializeFlowFromCommandsRequest,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.undo_events import (
    RedoRequest,
    RedoResultFailure,
    RedoResultSuccess,
    UndoRequest,
    UndoResultFailure,
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventSuppressionContext
from griptape_nodes.retained_mode.managers.undo.element_ids import element_ids, restore_element_ids
from griptape_nodes.retained_mode.managers.undo.run_state import carry_over_run_state
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.serialization.commands import decode_commands, encode_commands
from griptape_nodes.serialization.values import ValueEncodeError, dump_json

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.undo_history import Checkpoint
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")

# Fast enough to keep up with every step, and a tenth of the size of the uncompressed text.
_COMPRESSION_LEVEL = 1

# What checkpointing and rebuilding would broadcast: the results of the requests they make, such as
# deleting and recreating the workflow's nodes, and what the nodes report as they are set up.
_REBUILD_EVENTS = frozenset({ResultPayload, ExecutionPayload})


@dataclass(frozen=True)
class _Recording:
    """The step a request and everything it dispatches belong to."""

    workflow: WorkflowRuntime
    label: str


# Nested requests, and tasks a request starts, inherit the step through the context.
_recording: ContextVar[_Recording | None] = ContextVar("undo_recording", default=None)


@dataclass(frozen=True)
class DispatchScope:
    token: Token[_Recording | None]
    # The workflow whose step the request is part of, or None for a run, which is part of none.
    workflow: WorkflowRuntime | None


class UndoManager(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def begin_dispatch(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> DispatchScope | None:
        """Start recording a request that is part of a step. Call before its handler runs.

        Only an outermost request in an undo group is recorded: a request it dispatches belongs to
        it already. A request in a group other than the one being recorded starts a new step, so
        the workflow is checkpointed first, while it still shows the end of the step before.
        Everything a run changes is taken as a change outside any step.
        """
        if isinstance(request, RunControlMixin):
            if _recording.get() is None:
                return None
            return DispatchScope(token=_recording.set(None), workflow=None)
        if not outermost or workflow is None or request.undo_group is None:
            return None
        history = workflow.undo
        if history.restoring or isinstance(request, (UndoRequest, RedoRequest)):
            return None
        if request.undo_group != history.open_group:
            self._checkpoint(workflow)
            history.open_group = request.undo_group
        label = request.undo_label or _label_for(request)
        token = _recording.set(_Recording(workflow=workflow, label=label))
        history.in_flight += 1
        return DispatchScope(token=token, workflow=workflow)

    def end_dispatch(self, scope: DispatchScope | None) -> None:
        if scope is None:
            return
        _recording.reset(scope.token)
        if scope.workflow is not None:
            scope.workflow.undo.in_flight -= 1

    def note_altered(self, workflow: WorkflowRuntime, request: RequestPayload) -> None:
        """Note that a request changed ``workflow``, as its result reported."""
        history = workflow.undo
        # An undo or redo leaves the workflow at a checkpoint, which its own result reports as a change.
        if history.restoring or isinstance(request, (UndoRequest, RedoRequest)):
            return
        recording = _recording.get()
        if recording is not None and recording.workflow is workflow:
            # A label sent later in the group names the step. The first request names it otherwise.
            if request.undo_label is not None:
                history.pending_label = request.undo_label
            elif history.pending_label is None:
                history.pending_label = recording.label
        else:
            history.stale = True

    @handles(UndoRequest)
    async def on_undo_request(self, request: UndoRequest, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG002
        blocker = _blocker(workflow, "undo")
        if blocker is not None:
            return UndoResultFailure(result_details=_routine(blocker))
        if workflow.undo.undo_label is None:
            return UndoResultFailure(
                result_details=_routine("Attempted to undo. Failed because there is nothing to undo.")
            )
        # Checkpointed even when nothing reported a change, so the current checkpoint is the
        # workflow as it is, and a redo after this undo comes back to it.
        live = self._serialize(workflow)
        if live is None or not self._record(workflow, live):
            return UndoResultFailure(
                result_details=_routine("Attempted to undo. Failed because there is nothing to undo.")
            )
        history = workflow.undo
        previous = history.previous()
        current = history.current()
        if previous is None or current is None:
            return UndoResultFailure(
                result_details=_routine("Attempted to undo. Failed because there is nothing to undo.")
            )

        failure = await self._restore(workflow, previous, current, live)
        if failure is not None:
            return UndoResultFailure(result_details=f"Attempted to undo '{current.label}'. {failure}")
        history.step_back()
        self.engine.report_workflow_state(workflow)
        return UndoResultSuccess(label=current.label or "", result_details=f"Undid '{current.label}'.")

    @handles(RedoRequest)
    async def on_redo_request(self, request: RedoRequest, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG002
        blocker = _blocker(workflow, "redo")
        if blocker is not None:
            return RedoResultFailure(result_details=_routine(blocker))
        if workflow.undo.redo_label is None:
            return RedoResultFailure(
                result_details=_routine("Attempted to redo. Failed because there is nothing to redo.")
            )
        live = self._serialize(workflow)
        if live is None or not self._record(workflow, live):
            return RedoResultFailure(
                result_details=_routine("Attempted to redo. Failed because there is nothing to redo.")
            )
        following = workflow.undo.next()
        current = workflow.undo.current()
        if following is None or current is None:
            return RedoResultFailure(
                result_details=_routine("Attempted to redo. Failed because there is nothing to redo.")
            )

        failure = await self._restore(workflow, following, current, live)
        if failure is not None:
            return RedoResultFailure(result_details=f"Attempted to redo '{following.label}'. {failure}")
        workflow.undo.step_forward()
        self.engine.report_workflow_state(workflow)
        return RedoResultSuccess(label=following.label or "", result_details=f"Redid '{following.label}'.")

    def _checkpoint(self, workflow: WorkflowRuntime) -> None:
        """Take a checkpoint if the workflow may differ from the current one."""
        if not workflow.undo.needs_checkpoint:
            return
        commands = self._serialize(workflow)
        if commands is None:
            return
        self._record(workflow, commands)

    def _record(self, workflow: WorkflowRuntime, commands: SerializedFlowCommands) -> bool:
        """Checkpoint ``commands``, the workflow as it is now.

        Forgets the history and returns False when they cannot be written down, since the steps
        around them could no longer be undone faithfully.
        """
        history = workflow.undo
        try:
            text = dump_json(encode_commands(commands))
        except ValueEncodeError as err:
            logger.warning(
                "Undo could not checkpoint the workflow, so the changes made so far cannot be undone: %s", err
            )
            history.clear()
            self.engine.report_workflow_state(workflow)
            return False
        history.record(zlib.compress(text.encode(), _COMPRESSION_LEVEL))
        self.engine.report_workflow_state(workflow)
        return True

    def _serialize(self, workflow: WorkflowRuntime) -> SerializedFlowCommands | None:
        flow = workflow.top_level_flow()
        if flow is None:
            return None
        # The requests a serialization makes along the way are of no interest to clients.
        with EventSuppressionContext(self.engine.event_manager, _REBUILD_EVENTS):
            result = workflow.handle_request(SerializeFlowToCommandsRequest(flow_name=flow.name))
        if not isinstance(result, SerializeFlowToCommandsResultSuccess):
            logger.warning("Undo could not checkpoint the workflow: %s", result.result_details)
            workflow.undo.clear()
            self.engine.report_workflow_state(workflow)
            return None
        return result.serialized_flow_commands

    async def _restore(
        self, workflow: WorkflowRuntime, checkpoint: Checkpoint, current: Checkpoint, live: SerializedFlowCommands
    ) -> str | None:
        """Rebuild the workflow from ``checkpoint``, keeping ``live``'s results. Return why it failed, if it did.

        A rebuild that fails puts the workflow back as the ``current`` checkpoint holds it, which is
        ``live`` without its results, and forgets the history.
        """
        target = carry_over_run_state(_decode(checkpoint.commands), live)
        history = workflow.undo
        history.restoring = True
        try:
            failure = await self._rebuild(workflow, target)
            if failure is None:
                return None
            logger.error("Undo could not rebuild the workflow, so it is put back as it was: %s", failure)
            history.clear()
            recovery_failure = await self._rebuild(workflow, _decode(current.commands))
            if recovery_failure is not None:
                logger.error("Undo could not put the workflow back as it was: %s", recovery_failure)
                return f"Failed because the workflow could not be rebuilt, and is left incomplete: {failure}"
            return f"Failed because the workflow could not be rebuilt, so it was left as it was: {failure}"
        finally:
            history.restoring = False

    async def _rebuild(self, workflow: WorkflowRuntime, commands: SerializedFlowCommands) -> str | None:
        # Clients see the undo's own result and read the workflow again. Every node coming down and
        # back up along the way would only have them redraw it piece by piece, and react to it.
        ids = element_ids(workflow)
        with EventSuppressionContext(self.engine.event_manager, _REBUILD_EVENTS):
            flow = workflow.top_level_flow()
            if flow is not None:
                deleted = await workflow.ahandle_request(DeleteFlowRequest(flow_name=flow.name))
                if deleted.failed():
                    return str(deleted.result_details)
            rebuilt = await workflow.ahandle_request(
                DeserializeFlowFromCommandsRequest(serialized_flow_commands=commands)
            )
        if rebuilt.failed():
            return str(rebuilt.result_details)
        # So that clients can tell what is unchanged, and their requests by element id still work.
        restore_element_ids(workflow, ids)
        return None


def _blocker(workflow: WorkflowRuntime, verb: str) -> str | None:
    """Why an undo or redo cannot run now, or None when it can."""
    if workflow.flows.run_in_progress is not None:
        return f"Attempted to {verb}. Failed because the workflow is running."
    if workflow.undo.in_flight > 0:
        return f"Attempted to {verb}. Failed because the last change is still being made."
    if workflow.undo.restoring:
        return f"Attempted to {verb}. Failed because an undo or redo is already in progress."
    return None


def _routine(message: str) -> ResultDetails:
    """A failure the user only needs to be told of, such as pressing Undo with nothing to undo."""
    return ResultDetails(message=message, level=logging.INFO)


def _decode(commands: bytes) -> SerializedFlowCommands:
    return decode_commands(json.loads(zlib.decompress(commands)), SerializedFlowCommands)


def _label_for(request: RequestPayload) -> str:
    """Name a step after its request, such as "Set Parameter Value" for a SetParameterValueRequest."""
    name = type(request).__name__.removesuffix("Request")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
