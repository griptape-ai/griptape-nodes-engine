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
    """The step a request and everything it dispatches belong to.

    Args:
        workflow: The workflow the step changes.
        label: What the step is called, or None for a client's change outside any step.
    """

    workflow: WorkflowRuntime
    label: str | None


# Nested requests, and tasks a request starts, inherit the step through the context.
_recording: ContextVar[_Recording | None] = ContextVar("undo_recording", default=None)


@dataclass(frozen=True)
class DispatchScope:
    token: Token[_Recording | None]
    # The workflow whose step the request is part of, or None for a run, which is part of none.
    workflow: WorkflowRuntime | None
    # Whether the request is part of a step, rather than a change outside any step.
    in_step: bool = True


class UndoManager(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def begin_dispatch(
        self, request: RequestPayload, workflow: WorkflowRuntime | None, *, outermost: bool
    ) -> DispatchScope | None:
        """Start recording a request that is part of a step. Call before its handler runs.

        Only an outermost request is recorded: a request it dispatches belongs to it already. A
        request in a group other than the one being recorded starts a new step, so the workflow is
        checkpointed first, while it still shows the end of the step before. A request in no group
        is a change outside any step. Everything a run changes is taken as a change outside any
        step too, but only a client's change drops what could be redone.
        """
        if isinstance(request, RunControlMixin):
            if _recording.get() is None:
                return None
            return DispatchScope(token=_recording.set(None), workflow=None)
        if not outermost or workflow is None:
            return None
        history = workflow.undo
        if history.restoring or isinstance(request, (UndoRequest, RedoRequest)):
            return None
        if request.undo_group is None:
            return self._begin_change_outside_steps(workflow)
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
        if scope.workflow is None:
            return
        if scope.in_step:
            scope.workflow.undo.in_flight -= 1
            return
        # Checkpointed now rather than at the next step, so that a change that drops what could be
        # redone stops clients offering it at once.
        self._checkpoint(scope.workflow)

    def note_altered(self, workflow: WorkflowRuntime, request: RequestPayload) -> None:
        """Note that a request changed ``workflow``, as its result reported."""
        history = workflow.undo
        # An undo or redo leaves the workflow at a checkpoint, which its own result reports as a change.
        if history.restoring or isinstance(request, (UndoRequest, RedoRequest)):
            return
        recording = _recording.get()
        if recording is None or recording.workflow is not workflow:
            # A run's change, or a client's change made while there was nothing to redo.
            history.stale = True
        elif recording.label is None:
            history.stale = True
            history.edited = True
        # A label sent later in the group names the step. The first request names it otherwise.
        elif request.undo_label is not None:
            history.pending_label = request.undo_label
        elif history.pending_label is None:
            history.pending_label = recording.label

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

    def _begin_change_outside_steps(self, workflow: WorkflowRuntime) -> DispatchScope | None:
        """Start recording a client's request in no group, which drops what could be redone if it changes the workflow."""
        # With nothing to redo, it is only a change outside any step, which the next checkpoint takes in.
        if workflow.undo.redo_label is None:
            return None
        token = _recording.set(_Recording(workflow=workflow, label=None))
        return DispatchScope(token=token, workflow=workflow, in_step=False)

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
        compressed = zlib.compress(text.encode(), _COMPRESSION_LEVEL)
        current = history.current()
        # A redo would bring the workflow back without what a client changed since, so it is dropped.
        # A checkpoint holds no run results, so a run changes nothing a redo would lose.
        if history.edited and history.redo_label is not None and current is not None and current.commands != compressed:
            history.drop_redo()
        history.record(compressed)
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
    if workflow.undo.in_flight > 0:
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
