"""Undo and redo, by checkpointing what the user edited in each workflow and restoring a checkpoint.

A step is the changes made by requests that share an `undo_group`. The checkpoint at the end of a
step is taken when the next step starts, or when an undo or redo needs it, so a step of many requests
costs one serialization. A checkpoint is the workflow the way a save writes it, without what runs
produced and without workflow variables, neither of which an undo changes.

An undo brings the workflow to the checkpoint before by changing only the nodes that differ from it
(see `plan`). A node it rebuilds comes back through the code that opens a saved workflow, and a node
it keeps keeps its results and whatever it holds in memory. A kept node can still end up changed, by
what its rebuilt neighbors do as they connect to it, so the workflow is checked against the checkpoint
afterwards and any kept node that differs is rebuilt too.
"""

from __future__ import annotations

import json
import logging
import re
import zlib
from contextlib import asynccontextmanager, contextmanager
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
    SetFlowMetadataRequest,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    SerializedNodeCommands,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
    UnresolveNodeRequest,
)
from griptape_nodes.retained_mode.events.request_effects import never_alters_workflow
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
from griptape_nodes.retained_mode.managers.undo.plan import (
    RestorePlan,
    commands_to_build,
    plan_rebuild,
    plan_restore,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.retained_mode.runtime.undo_history import (
    OpenStep,
    OutsideEdit,
    RunOutput,
    StepPending,
    StepStillRunningError,
)
from griptape_nodes.serialization.commands import decode_commands, encode_commands
from griptape_nodes.serialization.values import ValueEncodeError, dump_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

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


@dataclass(eq=False)
class _OutsideEdit:
    """A client's request in no undo group, which drops what could be redone if it changes the workflow.

    Args:
        workflow: The workflow it acts on.
        running: Whether the request is still being handled. A task it started can outlive it.
    """

    workflow: WorkflowRuntime
    running: bool = True


# What the request being handled records into: the step it is part of, a change outside any step, or
# nothing, for a run. Requests it dispatches and tasks it starts inherit it through the context.
_recording: ContextVar[OpenStep | _OutsideEdit | None] = ContextVar("undo_recording", default=None)


class UndoDispatch:
    """Undo's part in handling one request: noting what the request changed."""

    def __init__(self, request: RequestPayload, workflow: WorkflowRuntime | None) -> None:
        self._request = request
        self._workflow = workflow

    def note(self, result: ResultPayload) -> None:
        """Note what ``result`` reports the request changed.

        Call before anything squelches its ``altered_workflow_state``, such as a load keeping the
        workflow from looking unsaved: the history has to know of every change, or an undo would
        take it back.
        """
        request = self._request
        if not result.altered_workflow_state:
            return
        if never_alters_workflow(type(request)):
            logger.error(
                "%s reported a change to the workflow, but its results say it never changes one, which "
                "undo relies on before it runs. Give it results that can report a change.",
                type(request).__name__,
            )
        if self._workflow is None:
            return
        history = self._workflow.undo
        # An undo or redo leaves the workflow at a checkpoint, which its own result reports as a change.
        if history.restoring or isinstance(request, (UndoRequest, RedoRequest)):
            return
        recording = _recording.get()
        if isinstance(recording, OpenStep) and recording is history.open_step and recording.running:
            history.note(StepPending(recording))
        elif isinstance(recording, _OutsideEdit) and recording.workflow is self._workflow and recording.running:
            history.note(OutsideEdit())
        else:
            # A run's change, or one a task made after the request that started it finished.
            history.note(RunOutput())


class UndoManager(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @asynccontextmanager
    async def dispatch(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> AsyncIterator[UndoDispatch]:
        """Wrap handling ``request``, which acts on ``workflow``, so the history records what it changes.

        A request that starts a new step first waits for the requests of the step before to finish,
        such as an agent's, since the step starts with a checkpoint that has to show the step before
        whole. Nothing awaits between the wait and the checkpoint, so no other request starts a step
        in between.
        """
        step = self._step_to_wait_for(request, workflow, outermost=outermost)
        while step is not None:
            await step.wait_until_idle()
            step = self._step_to_wait_for(request, workflow, outermost=outermost)
        with self._recording(request, workflow, outermost=outermost) as undo:
            yield undo

    @contextmanager
    def dispatch_now(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> Iterator[UndoDispatch]:
        """`dispatch` for a request handled synchronously, which cannot wait for the step before.

        Raises:
            StepStillRunningError: ``request`` would start a step while a request of the step
                before is still being handled.
        """
        if self._step_to_wait_for(request, workflow, outermost=outermost) is not None:
            msg = (
                f"Attempted to handle a {type(request).__name__}. Failed because another change to the workflow "
                "is still being made. Send it again once that change finishes."
            )
            raise StepStillRunningError(msg)
        with self._recording(request, workflow, outermost=outermost) as undo:
            yield undo

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
            # A failed rebuild forgets the history.
            self.engine.report_workflow_state(workflow)
            return UndoResultFailure(
                result_details=f"Attempted to undo '{current.label}'. {failure}", workflow_rebuilt=True
            )
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
            # A failed rebuild forgets the history.
            self.engine.report_workflow_state(workflow)
            return RedoResultFailure(
                result_details=f"Attempted to redo '{following.label}'. {failure}", workflow_rebuilt=True
            )
        workflow.undo.step_forward()
        self.engine.report_workflow_state(workflow)
        return RedoResultSuccess(label=following.label or "", result_details=f"Redid '{following.label}'.")

    def _step_to_wait_for(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> OpenStep | None:
        """The step before, if ``request`` would start a new step while one of that step's requests is being handled."""
        if workflow is None or request.undo_group is None or not self._records(request, workflow, outermost=outermost):
            return None
        step = workflow.undo.open_step
        if step is None or not step.running or step.group == request.undo_group:
            return None
        return step

    @contextmanager
    def _recording(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> Iterator[UndoDispatch]:
        """Record what ``request`` changes into what it is part of, while it is handled.

        Only an outermost request starts recording: a request it dispatches belongs to it already. A
        request in the open step's group continues that step, and a request in another group starts
        the next one, so the workflow is checkpointed first, while it still shows the end of the step
        before. A client's request in no group is a change outside any step. A run records into
        nothing, so what it changes is taken as a change outside any step that drops nothing.
        """
        token: Token[OpenStep | _OutsideEdit | None] | None = None
        recording = None
        if isinstance(request, RunControlMixin):
            token = _recording.set(None)
        else:
            recording = self._start_recording(request, workflow, outermost=outermost)
            if recording is not None:
                token = _recording.set(recording)
        try:
            yield UndoDispatch(request, workflow)
        finally:
            if token is not None:
                _recording.reset(token)
            if isinstance(recording, OpenStep):
                recording.request_finished()
            elif isinstance(recording, _OutsideEdit):
                recording.running = False
                # Checkpointed now rather than at the next step, so that a change that drops what could
                # be redone stops clients offering it at once.
                self._checkpoint(recording.workflow)

    def _start_recording(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> OpenStep | _OutsideEdit | None:
        """What ``request`` starts recording into, or None when it records into what it inherits."""
        if workflow is None or not self._records(request, workflow, outermost=outermost):
            return None
        if request.undo_group is not None:
            return self._continue_step(request, request.undo_group, workflow)
        # With nothing to redo, a change outside any step loses nothing, and the next checkpoint takes it in.
        if workflow.undo.redo_label is None:
            return None
        return _OutsideEdit(workflow)

    def _records(self, request: RequestPayload, workflow: WorkflowRuntime, *, outermost: bool) -> bool:
        """Whether ``request`` is recorded, as an outermost request a client sent that can change ``workflow``."""
        return (
            outermost
            and not isinstance(request, (RunControlMixin, UndoRequest, RedoRequest))
            and not never_alters_workflow(type(request))
            and not workflow.undo.restoring
        )

    def _continue_step(self, request: RequestPayload, group: str, workflow: WorkflowRuntime) -> OpenStep:
        """The step ``request``, in undo group ``group``, is part of, starting it if it is the first of its group."""
        history = workflow.undo
        step = history.open_step
        if step is None or step.group != group:
            self._checkpoint(workflow)
            step = OpenStep(group=group, label=request.undo_label or _label_for(request))
            history.open_step = step
        elif request.undo_label is not None:
            # A label sent later in the group names the step.
            step.label = request.undo_label
        step.request_started()
        return step

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
        commands = self._serialize_edits(workflow, flow.name)
        if commands is None:
            workflow.undo.clear()
            self.engine.report_workflow_state(workflow)
        return commands

    def _serialize_edits(self, workflow: WorkflowRuntime, flow_name: str) -> SerializedFlowCommands | None:
        """The workflow as a checkpoint holds it, or None when it could not be serialized."""
        # The requests a serialization makes along the way are of no interest to clients.
        with EventSuppressionContext(self.engine.event_manager, _REBUILD_EVENTS):
            result = workflow.handle_request(
                SerializeFlowToCommandsRequest(flow_name=flow_name, include_run_results=False)
            )
        if not isinstance(result, SerializeFlowToCommandsResultSuccess):
            logger.warning("Undo could not checkpoint the workflow: %s", result.result_details)
            return None
        return _without_variables(result.serialized_flow_commands)

    async def _restore(
        self, workflow: WorkflowRuntime, checkpoint: Checkpoint, current: Checkpoint, live: SerializedFlowCommands
    ) -> str | None:
        """Bring the workflow, which ``live`` holds, to ``checkpoint``. Return why it failed, if it did.

        A restore that fails puts the workflow back as the ``current`` checkpoint holds it, which is
        ``live``, rebuilt without its results, and forgets the history.
        """
        history = workflow.undo
        history.restoring = True
        try:
            # Clients see the undo's own result and read the workflow again. Every node coming down and
            # back up along the way would only have them redraw it piece by piece, and react to it.
            with EventSuppressionContext(self.engine.event_manager, _REBUILD_EVENTS):
                failure = await self._bring_to(workflow, _decode(checkpoint.commands), live)
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

    async def _bring_to(
        self, workflow: WorkflowRuntime, target: SerializedFlowCommands, live: SerializedFlowCommands
    ) -> str | None:
        """Change the nodes that differ from ``target``, until the workflow matches it. Return why it failed, if it did."""
        flow = workflow.top_level_flow()
        if flow is None:
            return "Failed because the workflow has no flow."
        ids = element_ids(workflow)
        plan = plan_restore(target, live)
        built: set[str] = set()
        # Each pass rebuilds at least one kept node, or ends, so there are at most as many passes as nodes.
        while not plan.changes_nothing:
            failure = await self._apply(workflow, flow.name, target, plan)
            if failure is not None:
                return failure
            built |= plan.units_to_build
            now = self._serialize_edits(workflow, flow.name)
            if now is None:
                return "Failed because the workflow could not be read back."
            check = plan_restore(target, now)
            # A rebuilt node is as close to the checkpoint as opening a saved workflow gets it.
            drifted = (check.units_to_build | check.node_edits.keys()) - built
            if not drifted:
                break
            plan = plan_rebuild(target, now, drifted)
        # So that clients can tell what is unchanged, and their requests by element id still work.
        restore_element_ids(workflow, ids)
        return None

    async def _apply(
        self, workflow: WorkflowRuntime, flow_name: str, target: SerializedFlowCommands, plan: RestorePlan
    ) -> str | None:
        """Make the changes ``plan`` holds. Return why it failed, if it did."""
        flow = workflow.top_level_flow()
        if flow is None:
            return "Failed because the workflow has no flow."
        # The checkpoint's nodes are built into the flow in context.
        with workflow.context.flow(flow):
            for request in _requests_for(flow_name, target, plan):
                result = await workflow.ahandle_request(request)
                if result.failed():
                    return str(result.result_details)
        return None

    async def _rebuild(self, workflow: WorkflowRuntime, commands: SerializedFlowCommands) -> str | None:
        """Delete the workflow's flow and build it again from ``commands``. Return why it failed, if it did."""
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


def _requests_for(flow_name: str, target: SerializedFlowCommands, plan: RestorePlan) -> list[RequestPayload]:
    """The requests that make the changes ``plan`` holds, in order."""
    requests: list[RequestPayload] = [
        DeleteNodeRequest(node_name=name) for unit in plan.units_to_delete for name in unit
    ]
    if plan.units_to_build:
        requests.append(
            DeserializeFlowFromCommandsRequest(
                serialized_flow_commands=commands_to_build(target, plan),
                existing_nodes={SerializedNodeCommands.NodeUUID(name): name for name in plan.kept},
            )
        )
        # Built nodes start unresolved. What they feed has to run again too.
        requests.extend(UnresolveNodeRequest(node_name=name) for name in sorted(plan.units_to_build))
    for name, edit in sorted(plan.node_edits.items()):
        if edit.metadata is not None:
            requests.append(
                SetNodeMetadataRequest(node_name=name, metadata=edit.metadata, remove_keys=edit.removed_metadata_keys)
            )
        if edit.lock is not None:
            requests.append(SetLockNodeStateRequest(node_name=name, lock=edit.lock))
    if plan.flow_metadata is not None:
        requests.append(
            SetFlowMetadataRequest(
                flow_name=flow_name, metadata=plan.flow_metadata, remove_keys=plan.removed_flow_metadata_keys
            )
        )
    return requests


def _blocker(workflow: WorkflowRuntime, verb: str) -> str | None:
    """Why an undo or redo cannot run now, or None when it can."""
    if workflow.flows.run_in_progress is not None:
        return f"Attempted to {verb}. Failed because the workflow is running."
    if workflow.undo.step_running:
        return f"Attempted to {verb}. Failed because the last change is still being made."
    if workflow.undo.restoring:
        return f"Attempted to {verb}. Failed because an undo or redo is already in progress."
    return None


def _routine(message: str) -> ResultDetails:
    """A failure the user only needs to be told of, such as pressing Undo with nothing to undo."""
    return ResultDetails(message=message, level=logging.INFO)


def _without_variables(commands: SerializedFlowCommands) -> SerializedFlowCommands:
    """``commands`` without the workflow variables, which belong to the workflow rather than its flow.

    Changes ``commands`` in place.
    """
    levels = _levels(commands)
    referenced = {
        command.unique_value_uuid
        for level in levels
        for set_value_commands in level.set_parameter_value_commands.values()
        for command in set_value_commands
    }
    for level in levels:
        level.serialized_variable_commands = []
        level.unique_parameter_uuid_to_values = {
            value_uuid: value
            for value_uuid, value in level.unique_parameter_uuid_to_values.items()
            if value_uuid in referenced
        }
    return commands


def _levels(commands: SerializedFlowCommands) -> list[SerializedFlowCommands]:
    levels = [commands]
    for sub_flow_commands in commands.sub_flows_commands:
        levels.extend(_levels(sub_flow_commands))
    return levels


def _decode(commands: bytes) -> SerializedFlowCommands:
    return decode_commands(json.loads(zlib.decompress(commands)), SerializedFlowCommands)


def _label_for(request: RequestPayload) -> str:
    """Name a step after its request, such as "Set Parameter Value" for a SetParameterValueRequest."""
    name = type(request).__name__.removesuffix("Request")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
