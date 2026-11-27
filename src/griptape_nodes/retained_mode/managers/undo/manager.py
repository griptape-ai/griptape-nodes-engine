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
from enum import Enum
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode
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
    NotEdits,
    RestorePlan,
    commands_to_build,
    levels,
    plan_rebuild,
    plan_restore,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.retained_mode.runtime.undo_history import (
    Checkpointed,
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
    from griptape_nodes.retained_mode.runtime.undo_history import Checkpoint, UndoHistory
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")

# Fast enough to keep up with every step, and a tenth of the size of the uncompressed text.
_COMPRESSION_LEVEL = 1

# What checkpointing and rebuilding would broadcast: the results of the requests they make, such as
# deleting and recreating the workflow's nodes, and what the nodes report as they are set up.
_REBUILD_EVENTS = frozenset({ResultPayload, ExecutionPayload})


@dataclass(eq=False)
class _UngroupedRequest:
    """A client's request in no undo group, which drops what could be redone if it changes the workflow.

    Args:
        workflow: The workflow it acts on.
        running: Whether the request is still being handled. A task it started can outlive it.
    """

    workflow: WorkflowRuntime
    running: bool = True


@dataclass(frozen=True)
class _Live:
    """The workflow as an undo or redo found it, and what checkpointing it did."""

    commands: SerializedFlowCommands
    checkpointed: Checkpointed


class _Way(Enum):
    """Whether a restore takes the last step back or puts the last one taken back again."""

    UNDO = "undo"
    REDO = "redo"

    def offered(self, history: UndoHistory) -> str | None:
        """What the history offers to take back or put back, or None when there is nothing."""
        if self is _Way.UNDO:
            return history.undo_label
        return history.redo_label

    def target(self, history: UndoHistory) -> Checkpoint | None:
        """The checkpoint to restore. Take a checkpoint first, so the current one is up to date."""
        if self is _Way.UNDO:
            return history.previous()
        return history.next()

    def step_label(self, target: Checkpoint, current: Checkpoint) -> str:
        """What the step between ``current`` and ``target`` is called, which the later of the two holds."""
        if self is _Way.UNDO:
            return current.label or ""
        return target.label or ""

    def arrive(self, history: UndoHistory, rebuilt: bytes) -> None:
        """Make the target current, as ``rebuilt``, the workflow the restore left."""
        if self is _Way.UNDO:
            history.step_back(rebuilt)
            return
        history.step_forward(rebuilt)

    def success(self, label: str) -> ResultPayload:
        if self is _Way.UNDO:
            return UndoResultSuccess(label=label, result_details=f"Undid '{label}'.")
        return RedoResultSuccess(label=label, result_details=f"Redid '{label}'.")

    def failure(self, details: ResultDetails | str, *, workflow_rebuilt: bool = False) -> ResultPayload:
        if self is _Way.UNDO:
            return UndoResultFailure(result_details=details, workflow_rebuilt=workflow_rebuilt)
        return RedoResultFailure(result_details=details, workflow_rebuilt=workflow_rebuilt)


# What the request being handled records into: the step it is part of, a change outside any step, or
# nothing, for a run. Requests it dispatches and tasks it starts inherit it through the context.
_recording: ContextVar[OpenStep | _UngroupedRequest | None] = ContextVar("undo_recording", default=None)


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
        elif isinstance(recording, _UngroupedRequest) and recording.workflow is self._workflow and recording.running:
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
        return await self._move(workflow, _Way.UNDO)

    @handles(RedoRequest)
    async def on_redo_request(self, request: RedoRequest, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG002
        return await self._move(workflow, _Way.REDO)

    async def _move(self, workflow: WorkflowRuntime, way: _Way) -> ResultPayload:
        """Take back the last step, or put back the last one taken back, as ``way`` says."""
        blocker = _blocker(workflow, way.value)
        if blocker is not None:
            return way.failure(_routine(blocker))
        nothing = _routine(f"Attempted to {way.value}. Failed because there is nothing to {way.value}.")
        history = workflow.undo
        offered = way.offered(history)
        if offered is None:
            return way.failure(nothing)
        live = self._checkpoint_live(workflow)
        if live is None:
            return way.failure(nothing)
        if live.checkpointed is Checkpointed.UNCHANGED:
            # The step an undo names put back what it changed, so it added no checkpoint. Taking it back
            # changes nothing, rather than taking back the step before it. A redo never finds this: a
            # pending step leaves nothing to redo.
            history.close_step()
            self.engine.report_workflow_state(workflow)
            return way.success(offered)
        return await self._restore_step(workflow, way, live.commands, nothing)

    async def _restore_step(
        self, workflow: WorkflowRuntime, way: _Way, live: SerializedFlowCommands, nothing: ResultDetails
    ) -> ResultPayload:
        """Restore the checkpoint ``way`` leads to from ``live``, the workflow just checkpointed."""
        history = workflow.undo
        current = history.current()
        target = way.target(history)
        if current is None or target is None:
            return way.failure(nothing)
        label = way.step_label(target, current)
        restored = await self._restore(workflow, target, current, live)
        if isinstance(restored, str):
            # A failed rebuild forgets the history.
            self.engine.report_workflow_state(workflow)
            return way.failure(f"Attempted to {way.value} '{label}'. {restored}", workflow_rebuilt=True)
        rebuilt = self._compress(workflow, restored)
        # Without it, the history is forgotten (see `_compress`).
        if rebuilt is not None:
            way.arrive(history, rebuilt)
        self.engine.report_workflow_state(workflow)
        return way.success(label)

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
        token: Token[OpenStep | _UngroupedRequest | None] | None = None
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
            elif isinstance(recording, _UngroupedRequest):
                recording.running = False
                # Checkpointed now rather than at the next step, so that a change that drops what could
                # be redone stops clients offering it at once.
                if recording.workflow.undo.redo_label is not None:
                    self._checkpoint(recording.workflow)

    def _start_recording(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> OpenStep | _UngroupedRequest | None:
        """What ``request`` starts recording into, or None when it records into what it inherits."""
        if workflow is None or not self._records(request, workflow, outermost=outermost):
            return None
        if request.undo_group is not None:
            return self._continue_step(request, request.undo_group, workflow)
        return _UngroupedRequest(workflow)

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

    def _checkpoint_live(self, workflow: WorkflowRuntime) -> _Live | None:
        """Checkpoint the workflow as it is, even when nothing reported a change, so an undo or redo starts from it.

        Returns None when it could not be checkpointed, which forgets the history.
        """
        commands = self._serialize(workflow)
        if commands is None:
            return None
        checkpointed = self._record(workflow, commands)
        if checkpointed is None:
            return None
        return _Live(commands=commands, checkpointed=checkpointed)

    def _record(self, workflow: WorkflowRuntime, commands: SerializedFlowCommands) -> Checkpointed | None:
        """Checkpoint ``commands``, the workflow as it is now, and return what that did.

        Forgets the history and returns None when they cannot be written down, since the steps
        around them could no longer be undone faithfully.
        """
        compressed = self._compress(workflow, commands)
        if compressed is None:
            return None
        not_edits = _not_edits(workflow)

        def differs(checkpoint: bytes) -> bool:
            if checkpoint == compressed:
                return False
            return not plan_restore(_decode(checkpoint), commands, not_edits).changes_nothing

        checkpointed = workflow.undo.record(compressed, differs=differs)
        self.engine.report_workflow_state(workflow)
        return checkpointed

    def _compress(self, workflow: WorkflowRuntime, commands: SerializedFlowCommands) -> bytes | None:
        """``commands`` as a checkpoint holds them, or None, forgetting the history, when they cannot be written down."""
        try:
            text = dump_json(encode_commands(commands))
        except ValueEncodeError as err:
            logger.warning(
                "Undo could not checkpoint the workflow, so the changes made so far cannot be undone: %s", err
            )
            workflow.undo.clear()
            self.engine.report_workflow_state(workflow)
            return None
        return zlib.compress(text.encode(), _COMPRESSION_LEVEL)

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
    ) -> SerializedFlowCommands | str:
        """Bring the workflow, which ``live`` holds, to ``checkpoint``. Return the workflow as it is then, or why it failed.

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
            if not isinstance(failure, str):
                return failure
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
    ) -> SerializedFlowCommands | str:
        """Change the nodes that differ from ``target``, until the workflow matches it.

        Return the workflow as it is then, or why it failed.
        """
        flow = workflow.top_level_flow()
        if flow is None:
            return "Failed because the workflow has no flow."
        ids = element_ids(workflow)
        plan = plan_restore(target, live, _not_edits(workflow))
        reached = live
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
            reached = now
            check = plan_restore(target, now, _not_edits(workflow))
            # A rebuilt node is as close to the checkpoint as opening a saved workflow gets it.
            drifted = (check.units_to_build | check.node_edits.keys()) - built
            if not drifted:
                break
            plan = plan_rebuild(target, now, drifted)
        # So that clients can tell what is unchanged, and their requests by element id still work.
        restore_element_ids(workflow, ids)
        return reached

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


def _not_edits(workflow: WorkflowRuntime) -> NotEdits:
    """Each node's parameters whose value a run or a connection set, as `plan_restore` leaves them out."""
    nodes = workflow.objects.get_filtered_subset(type=BaseNode).values()
    return {node.name: node.parameters_set_by_run_or_connection for node in nodes}


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
    all_levels = levels(commands)
    referenced = {
        command.unique_value_uuid
        for level in all_levels
        for set_value_commands in level.set_parameter_value_commands.values()
        for command in set_value_commands
    }
    for level in all_levels:
        level.serialized_variable_commands = []
        level.unique_parameter_uuid_to_values = {
            value_uuid: value
            for value_uuid, value in level.unique_parameter_uuid_to_values.items()
            if value_uuid in referenced
        }
    return commands


def _decode(commands: bytes) -> SerializedFlowCommands:
    return decode_commands(json.loads(zlib.decompress(commands)), SerializedFlowCommands)


def _label_for(request: RequestPayload) -> str:
    """Name a step after its request, such as "Set Parameter Value" for a SetParameterValueRequest."""
    name = type(request).__name__.removesuffix("Request")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
