from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowMetadataError,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.context_events import (
    CloseWorkflowRequest,
    CloseWorkflowResultFailure,
    CloseWorkflowResultSuccess,
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultFailure,
    EnsureWorkflowAndFlowResultSuccess,
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    ListOpenWorkflowsRequest,
    ListOpenWorkflowsResultSuccess,
    SetWorkflowContextFailure,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
from griptape_nodes.retained_mode.managers.event_manager import current_request_sender
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.retained_mode.runtime import context as runtime_context
from griptape_nodes.retained_mode.runtime.binding import bind_unbound_caller, bound_workflow, unbind_caller_closing
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextvars import Token
    from types import TracebackType

    from griptape_nodes.exe_types.core_types import BaseNodeElement
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine, OpeningWorkflow
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.binding import NoWorkflow
    from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ContextManager(EngineScoped):
    """Context manager for Workflow, Flow, Node, and Element contexts.

    Workflows own Flows, Flows own Nodes, and Nodes own Elements.
    The Workflow context is the current workflow (`Engine.current_workflow_or_none`), whose Flow,
    Node and Element contexts live on its runtime. A worker opens none: while it runs a node, the
    current workflow is the runtime it keeps for the node's orchestrator workflow
    (`adopt_orchestrator_workflow`). Workflows do not nest.
    Clients can push/pop Workflow contexts, Flow contexts, Node contexts, and Element contexts.
    """

    WorkflowContextError = runtime_context.WorkflowContextError
    EmptyStackError = runtime_context.EmptyStackError

    class NoActiveWorkflowError(WorkflowContextError):
        """No active workflow context error."""

    class NoActiveFlowError(WorkflowContextError):
        """No active flow context error."""

    class WorkflowAlreadyOpenError(WorkflowContextError):
        """A push asked to open a workflow while another one is in context."""

    class OrchestratorWorkflowError(WorkflowContextError):
        """A worker could not adopt the workflow its orchestrator ran a node for."""

    class WorkerWorkflowNotClosableError(WorkflowContextError):
        """Work on a worker tried to close the runtime the worker keeps for an orchestrator workflow."""

    class OpenRefusedError(WorkflowContextError):
        """An open was refused, having changed nothing. See `open_requested_workflow`."""

    class RequestedWorkflowNotFoundError(OpenRefusedError):
        """An open named an id or name that no registered workflow has."""

    class InvalidWorkingDirectoryError(OpenRefusedError):
        """An open's `working_directory` can't be the folder of a new workflow, because it names a file."""

    class WorkflowContext:
        """A context manager for a Workflow."""

        _manager: ContextManager
        _workflow_name: str

        def __init__(self, manager: ContextManager, workflow_name: str):
            self._manager = manager
            self._workflow_name = workflow_name
            self._binding: Token[WorkflowRuntime | NoWorkflow | None] | None = None

        def __enter__(self) -> None:
            workflow = self._manager.open_workflow(self._workflow_name)
            self._binding = bound_workflow.set(workflow)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            try:
                self._manager.pop_workflow()
            finally:
                if self._binding is not None:
                    bound_workflow.reset(self._binding)

    # The admittedly-confusing term for using these as a Python context (e.g., the `with` keyword)
    class FlowContext:
        """A context manager for a Flow."""

        _manager: ContextManager
        _flow: ControlFlow
        _exit_flow: Callable[[], None]

        def __init__(self, manager: ContextManager, flow: ControlFlow):
            self._manager = manager
            self._flow = flow

        def __enter__(self) -> ControlFlow:
            self._exit_flow = self._manager.enter_flow(self._flow)
            return self._flow

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self._exit_flow()

    class NodeContext:
        """A context manager for a Node within a Flow."""

        _manager: ContextManager
        _node: BaseNode

        def __init__(self, manager: ContextManager, node: BaseNode):
            self._manager = manager
            self._node = node

        def __enter__(self) -> BaseNode:
            return self._manager.push_node(self._node)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self._manager.pop_node()

    class ElementContext:
        """A context manager for an Element within a Node."""

        def __init__(self, manager: ContextManager, element: BaseNodeElement):
            self._manager = manager
            self._element = element

        def __enter__(self) -> BaseNodeElement:
            return self._manager.push_element(self._element)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self._manager.pop_element()

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(SetWorkflowContextRequest)
    def on_set_workflow_context_request(self, request: SetWorkflowContextRequest) -> ResultPayload:
        try:
            opened = self.open_requested_workflow(
                workflow_name=request.workflow_name,
                display_name=request.display_name,
                working_directory=request.working_directory,
            )
        except self.OpenRefusedError as err:
            return SetWorkflowContextFailure(result_details=str(err))
        resolved_name = self.engine.workflow_registry.get(opened.workflow.workflow_id).name
        if opened.already_open:
            msg = f"Attempted to open workflow '{resolved_name}'. Failed because it is already open."
            return SetWorkflowContextFailure(result_details=msg)

        msg = f"Successfully set the Workflow '{resolved_name}' as the Current Context."
        return SetWorkflowContextSuccess(
            workflow_name=resolved_name,
            workflow_id=opened.workflow.workflow_id,
            open_workflow=self.engine.open_workflow_info(opened.workflow),
            result_details=msg,
        )

    def open_requested_workflow(
        self,
        *,
        workflow_name: str | None = None,
        display_name: str | None = None,
        working_directory: str | None = None,
        create_flow: bool = False,
        flow_name: str | None = None,
    ) -> OpenedWorkflow:
        """Open the workflow a request names, as `opening_requested_workflow` does, with its top-level flow when `create_flow` is set.

        Refuses a workflow with a file that isn't open: this opens it without building its file, so
        it would show empty, and its next save would overwrite the file. `OpenWorkflowRequest`
        builds it. Clients hear of a workflow this opens once it has its flow. A flow that can't be
        created closes the workflow again (see `Engine.opening_workflow`).

        Raises:
            OpenRefusedError: As `opening_requested_workflow` raises, the workflow has a file and
                isn't open, or the flow could not be created.
        """
        entry = self._find_requested_workflow(None, workflow_name, None)
        if (
            entry is not None
            and entry.file_path is not None
            and self.engine.open_workflows.find(entry.workflow_id) is None
        ):
            msg = (
                f"Attempted to open workflow '{entry.name}'. Failed because this request can't load the "
                "nodes saved in its file. Open it with OpenWorkflowRequest instead."
            )
            raise self.OpenRefusedError(msg)
        with self.opening_requested_workflow(
            workflow_name=workflow_name, display_name=display_name, working_directory=working_directory
        ) as opened:
            if create_flow and not opened.already_open:
                self.create_top_level_flow_or_refuse(opened.workflow, flow_name)
        return opened

    @contextmanager
    def opening_requested_workflow(
        self,
        *,
        workflow_id: str | None = None,
        workflow_name: str | None = None,
        file_path: str | None = None,
        display_name: str | None = None,
        working_directory: str | None = None,
    ) -> Iterator[OpenedWorkflow]:
        """Open the workflow a request names to build into, or yield it as it is when it is already open. Never closes one.

        `OpenWorkflowRequest`, `SetWorkflowContextRequest`, `EnsureWorkflowAndFlowRequest`,
        `RunWorkflowFromRegistryRequest` and `RunWorkflowFromScratchRequest` open here. Name the
        workflow by at most one of `workflow_id`, `workflow_name` and `file_path`; naming none
        opens a new unsaved workflow. A workflow already open comes back unchanged: the arguments
        that only shape a new open (`display_name`, `working_directory`) are neither applied nor
        checked. Every refusal comes before anything is registered or opened.

        Clients hear of a workflow this opens once the block ends. A block that raises, or calls
        `OpenedWorkflow.abandon`, closes it again (see `Engine.opening_workflow`). A
        library-provided workflow is never open, so naming one always opens a new unsaved copy of it
        (see `open_workflow`), into which `open_and_build_workflow` builds the library's file.

        Args:
            workflow_id: Id of a registered workflow.
            workflow_name: Legacy registry name of a registered workflow.
            file_path: A workflow file. One the registry does not know is registered unlisted.
            display_name: Display name of a new unsaved workflow, "Untitled" when None.
            working_directory: Folder the workflow belongs to until it has a file, as
                SetWorkflowContextRequest describes.

        Raises:
            OpenRefusedError: The workflow was named more than one way, or `file_path` could not be
                registered.
            InvalidWorkingDirectoryError: `working_directory` names a file.
            RequestedWorkflowNotFoundError: No registered workflow has `workflow_id` or `workflow_name`.
        """
        named = [name for name in (workflow_id, workflow_name, file_path) if name is not None]
        if len(named) > 1:
            msg = "Attempted to open a workflow. Failed because the request named it more than one way. Give its id or its name, not both."
            raise self.OpenRefusedError(msg)
        entry = self._find_requested_workflow(workflow_id, workflow_name, file_path)
        if entry is not None:
            already_open = self.engine.open_workflows.find(entry.workflow_id)
            if already_open is not None:
                yield OpenedWorkflow(workflow=already_open, already_open=True)
                return

        normalized_directory = self._normalize_working_directory(working_directory)

        if entry is None:
            entry = self._register_requested_workflow(file_path, display_name)
        try:
            entry = self._entry_to_open(entry.name, file_path=None, working_directory=normalized_directory)
        except self.WorkflowAlreadyOpenError as err:
            raise self.OpenRefusedError(str(err)) from err
        with self.engine.opening_workflow(entry.workflow_id) as opening:
            yield OpenedWorkflow(workflow=opening.workflow, already_open=False, opening=opening)

    def create_top_level_flow_or_refuse(self, workflow: WorkflowRuntime, flow_name: str | None) -> None:
        """Create the top-level flow of `workflow`, which is being opened.

        Raises:
            OpenRefusedError: The flow could not be created. Raised inside an opening block, it
                closes the workflow again (see `Engine.opening_workflow`).
        """
        create_flow_result = self._create_top_level_flow(workflow, flow_name)
        if create_flow_result.succeeded():
            return
        workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name
        msg = f"Attempted to open workflow '{workflow_name}' with a flow. Failed to create the flow: {create_flow_result.result_details}"
        raise self.OpenRefusedError(msg)

    @handles(CloseWorkflowRequest)
    async def on_close_workflow_request(self, request: CloseWorkflowRequest) -> ResultPayload:
        workflow = self.engine.open_workflows.find(request.workflow_id)
        details = f"Workflow '{request.workflow_id}' is already closed."
        if workflow is not None:
            workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name
            try:
                await self.engine.aclose_workflow(workflow)
            except OpenWorkflowError as err:
                msg = f"Attempted to close workflow '{workflow_name}'. Failed because {err}"
                return CloseWorkflowResultFailure(result_details=msg)
            details = f"Closed workflow '{workflow_name}'."
        return CloseWorkflowResultSuccess(
            workflow_id=request.workflow_id, already_closed=workflow is None, result_details=details
        )

    @handles(ListOpenWorkflowsRequest)
    def on_list_open_workflows_request(self, request: ListOpenWorkflowsRequest) -> ResultPayload:  # noqa: ARG002
        open_workflows = self.engine.open_workflow_infos()
        return ListOpenWorkflowsResultSuccess(
            open_workflows=open_workflows,
            engine_run_id=self.engine.engine_run_id,
            result_details=f"Listed {len(open_workflows)} open workflow(s).",
        )

    @handles(GetWorkflowContextRequest)
    def on_get_workflow_context_request(
        self,
        request: GetWorkflowContextRequest,  # noqa: ARG002
        workflow: WorkflowRuntime,
    ) -> ResultPayload:
        entry = self.engine.workflow_registry.get(workflow.workflow_id)
        return GetWorkflowContextSuccess(
            workflow_name=entry.name,
            workflow_id=entry.workflow_id,
            is_saved=entry.is_saved,
            result_details=f"Successfully retrieved workflow context: {entry.name}",
        )

    @handles(EnsureWorkflowAndFlowRequest)
    def on_ensure_workflow_and_flow_request(
        self, request: EnsureWorkflowAndFlowRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """Cold-start bootstrap that guarantees a workflow + flow context exist.

        Uses the `workflow` the request names. A request that names none adopts the engine's only
        open workflow, and opens one with its flow when none is open, so callers can go from a blank
        engine to CreateNode in a single round trip. The result's `open_workflow` reports the
        workflow it used, opened or adopted, which an MCP session then keeps. This adoption is the one
        fallback to the only open workflow outside `GriptapeNodes.only_open_workflow_for_unbound_caller`.
        Another engine's request that names none acts on no workflow, so it never adopts one.
        """
        if workflow is not None:
            return self._ensure_flow(request, workflow)
        if current_request_sender() is not None:
            return self._open_workflow_with_flow(request)
        open_workflows = self.engine.open_workflows.all_open()
        if len(open_workflows) > 1:
            open_workflow_infos = self.engine.open_workflow_infos()
            listed = ", ".join(f"'{info.name}' ({info.workflow_id})" for info in open_workflow_infos)
            return WorkflowNotNamedResultFailure(
                open_workflows=open_workflow_infos,
                result_details=(
                    f"Attempted to ensure a workflow + flow context. Failed because the request names no workflow "
                    f"and {len(open_workflows)} are open: {listed}. Name one on the request envelope, or with the "
                    "X-Griptape-Workflow-Id header from an MCP client, or open one with OpenWorkflowRequest."
                ),
            )
        if open_workflows:
            adopted = open_workflows[0]
            return self._ensure_flow(request, adopted)
        return self._open_workflow_with_flow(request)

    def workflow(self, workflow_name: str) -> ContextManager.WorkflowContext:
        """Create a context manager for a Workflow context.

        Args:
            workflow_name: The name of the Workflow to enter.

        Returns:
            A context manager for the Workflow context.
        """
        return self.WorkflowContext(self, workflow_name)

    def flow(self, flow: ControlFlow | str) -> ContextManager.FlowContext:
        """Create a context manager for a Flow context.

        Args:
            flow: The Flow object to enter.

        Returns:
            A context manager for the Flow context.
        """
        if isinstance(flow, str):
            try:
                control_flow = self.engine.object_manager.attempt_get_object_by_name_as_type(flow, ControlFlow)
                if control_flow is None:
                    msg = f"Flow '{flow}' not found in current workflow."
                    raise ValueError(msg)
                flow = control_flow
            except KeyError as e:
                msg = f"Flow '{flow}' not found in current workflow."
                raise ValueError(msg) from e
        return self.FlowContext(self, flow)

    def node(self, node: str | BaseNode) -> ContextManager.NodeContext:
        """Create a context manager for a Node context.

        Args:
            node: The Node object to enter.

        Returns:
            A context manager for the Node context.
        """
        if isinstance(node, str):
            try:
                node = self.get_current_flow().nodes[node]
            except KeyError as e:
                msg = f"Node '{node}' not found in current flow."
                raise ValueError(msg) from e
        return self.NodeContext(self, node)

    def element(self, element: BaseNodeElement | str) -> ContextManager.ElementContext:
        """Create a context manager for an Element context.

        Args:
            element: The Element object to enter.

        Returns:
            A context manager for the Element context.
        """
        if isinstance(element, str):
            try:
                node_element = self.get_current_node().root_ui_element.find_element_by_name(element)
                if node_element is None:
                    msg = f"Element '{element}' not found in current node."
                    raise ValueError(msg)
                element = node_element
            except KeyError as e:
                msg = f"Element '{element}' not found in current node."
                raise ValueError(msg) from e
        return self.ElementContext(self, element)

    def adopt_orchestrator_workflow(
        self,
        workflow_id: str | None,
        name: str | None,
        file_path: str | None,
        working_directory: str | None,
    ) -> WorkflowRuntime:
        """The runtime this worker keeps for the workflow its orchestrator ran a node for.

        The node runs bound to it (`Engine.run_in_worker_workflow`), so its context and paths resolve
        against the orchestrator's workflow. The runtime is an ordinary `WorkflowRuntime` in
        `Engine.worker_workflows`, never an open workflow here.

        Its registry entry is this engine's entry under the orchestrator's workflow id, registered
        unlisted when this engine does not know the workflow, and moved when the orchestrator has
        saved or moved it since. `name` is the orchestrator's legacy name, used only for messages.
        The runtime and its entry go when the orchestrator tears its workflow down
        (`Engine.sweep_worker_workflows`).

        Raises:
            OrchestratorWorkflowError: A workflow is open in this engine; the orchestrator has no
                workflow open; the orchestrator named a workflow without its id; or the workflow
                could not be registered, which includes a node still running for its file under an
                earlier id.
        """
        if self.engine.open_workflows.all_open():
            msg = (
                "Attempted to run a node for another engine's workflow. "
                "Failed because a workflow is open in this engine."
            )
            raise self.OrchestratorWorkflowError(msg)

        if workflow_id is None and name is not None:
            # An orchestrator that names a workflow but sends no id predates workflow ids. Running in no
            # workflow would resolve the node's paths workspace-relative.
            msg = (
                f"Attempted to run a node for workflow '{name}'. Failed because the engine that sent it "
                f"did not say which workflow it is, which happens when it runs a different version of "
                f"Griptape Nodes. Run the same version on both."
            )
            raise self.OrchestratorWorkflowError(msg)

        if workflow_id is None:
            # The node's copy is built in the workflow it runs for, so there is nowhere to build it.
            msg = (
                "Attempted to run a node in a separate process. Failed because no workflow was open when it "
                "was sent. Open a workflow and run it again."
            )
            raise self.OrchestratorWorkflowError(msg)

        self._register_orchestrator_workflow(workflow_id, name, file_path, working_directory)
        return self.engine.worker_workflows.runtime_for(workflow_id)

    def _register_orchestrator_workflow(
        self,
        workflow_id: str,
        name: str | None,
        file_path: str | None,
        working_directory: str | None,
    ) -> None:
        """Make this engine's registry describe the orchestrator's workflow under the orchestrator's id.

        The peer's id is the authority. Ids last only while an engine runs and each engine mints
        its own, so this engine usually holds the peer's file under an id of its own. That entry is
        dropped and the file is registered again under the peer's id.

        A file that no longer exists is registered as a workflow without a file, in that file's folder.

        Raises:
            OrchestratorWorkflowError: Another workflow here already uses the peer's file, or a node
                is still running in the runtime kept for the file under an earlier id.
        """
        registry = self.engine.workflow_registry
        workflow = registry.find_by_id(workflow_id)

        if file_path is None:
            if workflow is None:
                workflow = registry.register_unsaved(
                    name or "Untitled", working_directory, workflow_id=workflow_id, listed=False
                )
            workflow.working_directory = working_directory
            return

        known = registry.find_by_path(file_path)
        if known is not None and known.workflow_id == workflow_id:
            return
        # An entry this engine's scan listed stays listed under the peer's id, so closing the
        # runtime kept for it later does not drop the file from this engine's workflows.
        keeps_listing = known is not None and known.listed
        if known is not None:
            self._drop_stale_orchestrator_entry(known, workflow_id, name)

        try:
            if workflow is not None:
                # The peer saved or moved the workflow since this engine last ran a node for it.
                registry.set_file_path(workflow, file_path)
                if keeps_listing:
                    workflow.listed = True
            else:
                opened = read_metadata_for_open_file(Path(file_path))
                registry.register_file(
                    file_path,
                    opened.metadata,
                    workflow_id=workflow_id,
                    listed=keeps_listing,
                    metadata_is_placeholder=opened.metadata_is_placeholder,
                )
        except ValueError as err:
            logger.debug("Could not register workflow '%s' from '%s': %s", workflow_id, file_path, err)
            self._register_unregistrable_file(err, workflow_id, name, file_path, known_here=workflow is not None)

    def _drop_stale_orchestrator_entry(self, known: Workflow, workflow_id: str, name: str | None) -> None:
        """Drop the entry this engine holds the peer's file under, an id other than the peer's, and its runtime.

        Raises:
            OrchestratorWorkflowError: A node is still running in the runtime kept under that id.
        """
        logger.debug(
            "Workflow '%s' was registered here as '%s'; the peer engine knows it as '%s'.",
            known.name,
            known.workflow_id,
            workflow_id,
        )
        # A runtime kept under the stale id goes with it; the caller keeps one under the peer's id.
        stale = self.engine.worker_workflows.find(known.workflow_id)
        if stale is not None:
            if self.engine.worker_workflows.is_running(stale):
                msg = (
                    f"Attempted to run a node of workflow '{name or workflow_id}'. Failed because a run of this "
                    "workflow from before its engine restarted is still running on this worker. Wait for it to "
                    "finish and try again."
                )
                raise self.OrchestratorWorkflowError(msg)
            self.engine.close_worker_workflow(stale)
        # Closing the runtime drops an unlisted entry; a listed one is dropped here.
        registry = self.engine.workflow_registry
        if registry.find_by_id(known.workflow_id) is not None:
            registry.remove(known.workflow_id)

    def _register_unregistrable_file(
        self, err: ValueError, workflow_id: str, name: str | None, file_path: str, *, known_here: bool
    ) -> None:
        """Register the peer's workflow when its file could not be registered here, or raise why not.

        A file that is gone was deleted or moved outside the engine, but the peer still has the
        workflow open at that path, so the workflow is registered with no file, in that file's
        folder, where the peer resolves its paths. An entry already known here under the peer's id
        stays.

        Raises:
            OrchestratorWorkflowError: The file exists and another workflow here already uses it.
        """
        if Path(file_path).exists():
            msg = (
                f"Attempted to run a node of workflow '{name or workflow_id}'. Failed because the workflow's "
                f"file '{file_path}' could not be opened: another workflow is already using that file."
            )
            raise self.OrchestratorWorkflowError(msg) from err
        if known_here:
            return
        self.engine.workflow_registry.register_unsaved(
            name or "Untitled", str(Path(file_path).parent), workflow_id=workflow_id, listed=False
        )

    def has_current_workflow(self) -> bool:
        """Check if there is an active Workflow context: the current workflow (`Engine.current_workflow_or_none`)."""
        return self._current_or_none() is not None

    def has_current_flow(self) -> bool:
        """Check if there is an active Flow context within the current Workflow."""
        if not self.has_current_workflow():
            return False

        current_workflow = self._current_context()
        return current_workflow.has_current_flow()

    def has_current_node(self) -> bool:
        """Check if there is an active Node within the current Flow."""
        if not self.has_current_flow():
            return False

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()
        return current_flow.has_current_node()

    def has_current_element(self) -> bool:
        """Check if there is an active Element within the current Node."""
        if not self.has_current_node():
            return False

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()
        current_node = current_flow._node_stack[-1]
        return current_node.has_current_element()

    def get_current_workflow_id(self) -> str:
        """Get the id of the current Workflow context.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        current = self._current_or_none()
        if current is None:
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return current.workflow_id

    def get_current_workflow(self) -> Workflow:
        """Get the registry entry of the current Workflow context.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        return self.engine.workflow_registry.get(self.get_current_workflow_id())

    def get_current_workflow_name(self) -> str:
        """Get the name of the current Workflow context.

        Returns:
            The legacy registry name of the current Workflow, read from its registry entry.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        return self.get_current_workflow().name

    def get_current_flow(self) -> ControlFlow:
        """Get the current Flow object.

        Returns:
            The current Flow object.

        Raises:
            NoActiveFlowError: If no Flow context is active.
        """
        if not self.has_current_flow():
            msg = "No active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        return current_workflow.get_current_flow()

    def get_current_node(self) -> BaseNode:
        """Get the name of the current Node within the current Flow.

        Returns:
            The name of the current Node.

        Raises:
            NoActiveFlowError: If no Flow context is active.
            EmptyStackError: If the current Flow has no active Nodes.
        """
        if not self.has_current_flow():
            msg = "No active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()
        return current_flow.get_current_node()

    def get_current_element(self) -> BaseNodeElement:
        """Get the name of the current element within the current node.

        Returns:
            The name of the current element.

        Raises:
            NoActiveFlowError: If no Flow context is active.
            EmptyStackError: If the current Flow has no active Nodes or Elements.
        """
        if not self.has_current_flow():
            msg = "No active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()

        if not current_flow.has_current_node():
            msg = "No active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.get_current_element()

    def push_workflow(
        self,
        workflow_name: str | None = None,
        *,
        file_path: str | None = None,
        working_directory: str | None = None,
    ) -> str:
        """Open a workflow as the Workflow context, and return its name. See `open_workflow`.

        A caller bound to nothing, or to a workflow that has closed, is bound to the opened workflow
        for the rest of its context: a saved workflow file run on its own opens itself this way
        (`push_workflow(file_path=__file__)`) and then sends its requests and reads naming no workflow.
        """
        opened = self.open_workflow(workflow_name, file_path=file_path, working_directory=working_directory)
        bind_unbound_caller(opened)
        return self.engine.workflow_registry.get(opened.workflow_id).name

    def open_workflow(
        self,
        workflow_name: str | None = None,
        *,
        file_path: str | None = None,
        working_directory: str | None = None,
    ) -> WorkflowRuntime:
        """Open a workflow as the Workflow context, through `Engine.open_workflow`, and return its runtime.

        The context holds only the workflow's id; its name, file and folder are read from its
        registry entry. Workflows do not nest: a push while the current work is bound to a live
        workflow is refused. Opening binds nothing (`push_workflow` binds an unbound caller): work
        that builds into the opened workflow binds it (`Engine.bind_workflow`).

        A library-provided workflow opens as a new unsaved copy of itself
        (`register_unsaved_copy`), so every open, a script's own included, leaves the library's
        entry closed. The caller builds the library's file into the copy.

        Args:
            workflow_name: The legacy registry name of the Workflow to enter. A name the registry
                does not hold registers a new unsaved Workflow with that display name, unlisted.
            file_path: Path to a workflow file. Saved workflow files push themselves this way
                (`push_workflow(file_path=__file__)`). A file the registry does not know is
                registered unlisted, so it does not appear in the workflow list. A path with no
                file registers an unsaved Workflow that belongs to the path's folder.
                Mutually exclusive with workflow_name.
            working_directory: Folder the Workflow belongs to while it has no file of its own,
                set on its registry entry. A DIRECTORY, not a file path. Never overrides the
                workflow's file: a saved workflow answers with that file's own directory.
                Expected to be absolute -- `on_set_workflow_context_request` normalizes before it
                reaches here.

        Returns:
            The runtime of the Workflow that was entered: the copy's, for a library workflow.

        Raises:
            ValueError: If neither or both of workflow_name and file_path are provided.
            WorkflowAlreadyOpenError: If the current work is bound to a live workflow.
        """
        entry = self._entry_to_open(workflow_name, file_path=file_path, working_directory=working_directory)
        return self.engine.open_workflow(entry.workflow_id)

    def _entry_to_open(
        self, workflow_name: str | None, *, file_path: str | None, working_directory: str | None
    ) -> Workflow:
        """The registry entry `open_workflow` opens, registered when there is none. See `open_workflow`.

        Raises:
            ValueError: If neither or both of workflow_name and file_path are provided.
            WorkflowAlreadyOpenError: If the current work is bound to a live workflow, or another
                workflow is open and only one may be.
        """
        if workflow_name is not None and file_path is not None:
            msg = "Provide either workflow_name or file_path, not both."
            raise ValueError(msg)
        blocking = self.engine.bound_live_workflow_or_none()
        if blocking is not None:
            msg = (
                f"Attempted to open workflow '{workflow_name or file_path}'. Failed because workflow "
                f"'{self.engine.workflow_registry.get(blocking.workflow_id).name}' is already open. "
                "Close or clear the open workflow first."
            )
            raise self.WorkflowAlreadyOpenError(msg)
        if workflow_name is not None:
            workflow = self.engine.workflow_registry.find_by_name(workflow_name)
            if workflow is None:
                # Unlisted, like a file registered only because it was opened; a save lists it.
                workflow = self.engine.workflow_registry.register_unsaved(workflow_name, listed=False)
        elif file_path is not None:
            workflow = self._find_or_register_open_file(file_path)
        else:
            msg = "Either workflow_name or file_path must be provided."
            raise ValueError(msg)

        # A library workflow never opens itself, so its file is never saved over.
        if workflow.metadata.is_griptape_provided:
            workflow = self.engine.workflow_registry.register_unsaved_copy(workflow)
        if working_directory is not None:
            workflow.working_directory = working_directory
        return workflow

    def _find_or_register_open_file(self, file_path: str) -> Workflow:
        """The registry entry for a workflow file being opened, registered unlisted when there is none.

        Unlisted, so running a file from outside the workspace does not add it to the workflow
        list. A file whose header cannot be read still opens (tests and scripts push their own
        `__file__`), with placeholder metadata its entry is marked as holding. A path with no file
        behind it opens as an unsaved workflow whose folder is the path's.
        """
        registry = self.engine.workflow_registry
        workflow = registry.find_by_path(file_path)
        if workflow is not None:
            return workflow
        path = registry.canonical_path(file_path)
        if not path.is_file():
            return registry.register_unsaved(path.stem, str(path.parent), listed=False)
        opened = read_metadata_for_open_file(path)
        return registry.register_file(
            path, opened.metadata, listed=False, metadata_is_placeholder=opened.metadata_is_placeholder
        )

    def pop_workflow(self) -> str:
        """Close the current workflow through `Engine.close_workflow`, which tears down what it holds.

        A caller `push_workflow` bound to it is unbound again.

        Returns:
            The name of the Workflow that was closed. `get_current_workflow_id` gives its id
            beforehand.

        Raises:
            EmptyStackError: If no workflow is current.
            WorkerWorkflowNotClosableError: If the current workflow is one this worker keeps for its
                orchestrator, which closes it when the orchestrator closes that workflow.
        """
        workflow = self.engine.current_workflow_or_none()
        if workflow is None:
            msg = "Cannot pop Workflow: no active Workflows"
            raise self.EmptyStackError(msg)
        name = self.get_current_workflow_name()
        if self.engine.worker_workflows.find(workflow.workflow_id) is workflow:
            msg = (
                f"Attempted to close workflow '{name}'. Failed because this worker runs nodes for it on behalf "
                "of the engine that has it open, and that engine closes it."
            )
            raise self.WorkerWorkflowNotClosableError(msg)

        self.engine.close_workflow(workflow)
        # Undo what `push_workflow` bound, so the caller is back where it was before it pushed.
        unbind_caller_closing(workflow)
        return name

    def enter_flow(self, flow: ControlFlow) -> Callable[[], None]:
        """Push a Flow context for a `with` block and return the action that exits it.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = f"Cannot enter a Flow context '{flow.name}' without an active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return self._current_context().enter_flow(flow)

    def push_flow(self, flow: ControlFlow) -> ControlFlow:
        """Push a new Flow context onto the stack for the current Workflow.

        Args:
            flow: The Flow object to enter.

        Returns:
            The Flow object that was entered.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = f"Cannot enter a Flow context '{flow.name}' without an active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        current_workflow = self._current_context()
        return current_workflow.push_flow(flow)

    def pop_flow(self) -> ControlFlow:
        """Pop the current Flow context from the stack.

        Returns:
            The Flow object that was popped.

        Raises:
            EmptyStackError: If no Flow is active.
        """
        if not self.has_current_workflow():
            msg = "Cannot pop Flow: stack is empty"
            raise self.EmptyStackError(msg)

        current_workflow = self._current_context()
        return current_workflow.pop_flow()

    def push_node(self, node: BaseNode) -> BaseNode:
        """Push a new Node context onto the stack for the current Flow.

        Args:
            node: The Node object to enter.

        Returns:
            The Node object that was entered.

        Raises:
            NoActiveFlowError: If no Flow context is active.
        """
        if not self.has_current_flow():
            msg = f"Cannot enter a Node context '{node.name}' without an active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()
        result = current_flow.push_node(node)
        return result

    def pop_node(self) -> BaseNode:
        """Pop the current Node context from the stack for the current Flow.

        Returns:
            The name of the Node that was popped.

        Raises:
            NoActiveFlowError: If no Flow context is active.
            EmptyStackError: If the current Flow has no active Nodes.
        """
        if not self.has_current_flow():
            msg = "Cannot pop Node: no active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()
        result = current_flow.pop_node()
        return result

    def push_element(self, element: BaseNodeElement) -> BaseNodeElement:
        """Push a new element onto the stack for the current node.

        Args:
            element: The Element object to enter.

        Returns:
            The Element object that was entered.

        Raises:
            NoActiveFlowError: If no Flow context is active.
            EmptyStackError: If the current Flow has no active Nodes or the current Node has no active Elements.
        """
        if not self.has_current_flow():
            msg = f"Cannot enter an Element context '{element.name}' without an active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()

        if not current_flow.has_current_node():
            msg = "Cannot enter an Element context without an active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.push_element(element)

    def pop_element(self) -> BaseNodeElement:
        """Pop the current element from the stack for the current node.

        Returns:
            The Element object that was popped.

        Raises:
            NoActiveFlowError: If no Flow context is active.
            EmptyStackError: If the current Flow has no active Nodes or Elements.
        """
        if not self.has_current_flow():
            msg = "Cannot pop Element: no active Flow context"
            raise self.NoActiveFlowError(msg)

        current_workflow = self._current_context()
        current_flow = current_workflow.current_flow_state()

        if not current_flow.has_current_node():
            msg = "Cannot pop Element: no active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.pop_element()

    def _open_workflow_with_flow(self, request: EnsureWorkflowAndFlowRequest) -> ResultPayload:
        """Open the workflow an Ensure request names, or a new one, with its top-level flow.

        A flow that can't be created closes the workflow again (see `Engine.opening_workflow`). A
        workflow already open is used as it is, with a flow created if
        it has none.
        """
        try:
            opened = self.open_requested_workflow(
                workflow_name=request.workflow_name,
                display_name=request.display_name,
                working_directory=request.working_directory,
                create_flow=True,
                flow_name=request.flow_name,
            )
        except self.OpenRefusedError as err:
            return EnsureWorkflowAndFlowResultFailure(
                result_details=f"Attempted to ensure a workflow + flow context. Failed while opening the workflow: {err}"
            )
        if opened.already_open:
            return self._ensure_flow(request, opened.workflow)

        workflow = opened.workflow
        workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name
        flow_name = workflow.context.get_current_flow().name
        return EnsureWorkflowAndFlowResultSuccess(
            workflow_name=workflow_name,
            workflow_id=workflow.workflow_id,
            flow_name=flow_name,
            created_workflow=True,
            created_flow=True,
            open_workflow=self.engine.open_workflow_info(workflow),
            result_details=(
                f"Workflow '{workflow_name}' and Flow '{flow_name}' are ready "
                "(created_workflow=True, created_flow=True)."
            ),
        )

    def _ensure_flow(self, request: EnsureWorkflowAndFlowRequest, workflow: WorkflowRuntime) -> ResultPayload:
        """Give the open `workflow` a top-level flow if it has none, for an Ensure request.

        Refuses a workflow another request is still building: its file creates the top-level flow,
        and a flow created first would become that flow's parent.
        """
        workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name
        if self.engine.is_opening(workflow):
            return EnsureWorkflowAndFlowResultFailure(
                result_details=(
                    f"Attempted to ensure a workflow + flow context in workflow '{workflow_name}'. "
                    "Failed because another request is still opening it. Try again once it has opened."
                )
            )
        created_flow = False
        if workflow.context.has_current_flow():
            flow_name = workflow.context.get_current_flow().name
        else:
            create_flow_result = self._create_top_level_flow(workflow, request.flow_name)
            if create_flow_result.failed():
                return EnsureWorkflowAndFlowResultFailure(
                    result_details=(
                        f"Attempted to ensure a workflow + flow context. Failed while creating the flow: "
                        f"{create_flow_result.result_details}"
                    )
                )
            flow_name = workflow.context.get_current_flow().name
            created_flow = True

        return EnsureWorkflowAndFlowResultSuccess(
            workflow_name=workflow_name,
            workflow_id=workflow.workflow_id,
            flow_name=flow_name,
            created_workflow=False,
            created_flow=created_flow,
            open_workflow=self.engine.open_workflow_info(workflow),
            result_details=(
                f"Workflow '{workflow_name}' and Flow '{flow_name}' are ready "
                f"(created_workflow=False, created_flow={created_flow})."
            ),
        )

    def _normalize_working_directory(self, working_directory: str | None) -> str | None:
        """`working_directory` made absolute against the workspace, or None.

        Raises:
            InvalidWorkingDirectoryError: It names an existing file.
        """
        if working_directory is None:
            return None
        # Normalized here rather than at read time so `workflow_dir` never has to care whether
        # the caller sent an absolute path, a workspace-relative one, or one with a `~` in it.
        normalized = str(canonicalize_for_identity(working_directory, base=self.engine.config_manager.workspace_path))
        # A file where a folder was meant is the one mistake worth rejecting: the value
        # becomes the parent of every path the workflow writes, so accepting it would put
        # outputs beside the file rather than in the folder the caller named -- a plausible
        # location, which is what makes it hard to notice.
        existing = Path(normalized)
        if existing.exists() and not existing.is_dir():
            msg = (
                f"Attempted to set the folder for a new Workflow to '{working_directory}'. "
                f"Failed because that path is a file, not a folder."
            )
            raise self.InvalidWorkingDirectoryError(msg)
        return normalized

    def _register_requested_workflow(self, file_path: str | None, display_name: str | None) -> Workflow:
        """Register the workflow an open names that the registry does not hold: the file at `file_path`, or a new unsaved one.

        Raises:
            OpenRefusedError: The file could not be registered.
        """
        if file_path is None:
            return self.engine.workflow_registry.register_unsaved(display_name or "Untitled")
        try:
            return self._find_or_register_open_file(file_path)
        except ValueError as err:
            msg = f"Attempted to open workflow file '{file_path}'. Failed because {err}"
            raise self.OpenRefusedError(msg) from err

    def _find_requested_workflow(
        self, workflow_id: str | None, workflow_name: str | None, file_path: str | None
    ) -> Workflow | None:
        """The registry entry an open names, or None for a new workflow or a file the registry does not know.

        Raises:
            RequestedWorkflowNotFoundError: No registered workflow has `workflow_id` or `workflow_name`.
        """
        registry = self.engine.workflow_registry
        if workflow_id is not None:
            entry = registry.find_by_id(workflow_id)
            if entry is None:
                msg = (
                    f"Attempted to open workflow '{workflow_id}'. Failed because no workflow has that id. "
                    "Workflow ids last only until the engine restarts, and an unsaved workflow is lost when it closes."
                )
                raise self.RequestedWorkflowNotFoundError(msg)
            return entry
        if workflow_name is not None:
            entry = registry.find_by_name(workflow_name)
            if entry is None:
                # A name no workflow has fails, rather than opening a blank workflow under it: a
                # typo, a name another client just renamed, or a file the workspace scan has not
                # registered yet would otherwise show an empty canvas whose next save writes a new
                # file.
                msg = (
                    f"Attempted to open workflow '{workflow_name}'. Failed because no workflow with that name "
                    f"is registered. Open a workflow from the workflow list, or start a new one."
                )
                raise self.RequestedWorkflowNotFoundError(msg)
            return entry
        if file_path is not None:
            return registry.find_by_path(file_path)
        return None

    def _create_top_level_flow(self, workflow: WorkflowRuntime, flow_name: str | None) -> ResultPayload:
        """Create `workflow`'s top-level flow, named `flow_name` or by the engine, and make it the current flow."""
        # Lazy import required: context_manager is imported by griptape_nodes, creating a circular dependency.
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest

        return workflow.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=flow_name, set_as_new_context=True)
        )

    def _current_context(self) -> WorkflowContextStack:
        """The Flow, Node and Element stacks of the Workflow context.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        current = self._current_or_none()
        if current is None:
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)
        return current.context

    def _current_or_none(self) -> WorkflowRuntime | None:
        """The current workflow (`Engine.current_workflow_or_none`), or None when there is none."""
        return self.engine.current_workflow_or_none()


@dataclass(frozen=True)
class OpenedWorkflow:
    """The workflow `ContextManager.opening_requested_workflow` yielded.

    Args:
        workflow: Its runtime.
        already_open: Whether it was open before, and so was yielded unchanged.
        opening: The open of a workflow that was not open before (see `Engine.opening_workflow`).
    """

    workflow: WorkflowRuntime
    already_open: bool
    opening: OpeningWorkflow | None = None

    def abandon(self) -> None:
        """Close the workflow once the block ends, instead of telling clients of it. Leaves one already open as it is."""
        if self.opening is not None:
            self.opening.abandon()


class OpenedFileMetadata(NamedTuple):
    """The metadata a workflow file opens with, and whether it is a placeholder for a header that could not be read."""

    metadata: WorkflowMetadata
    metadata_is_placeholder: bool


def read_metadata_for_open_file(file_path: Path) -> OpenedFileMetadata:
    """The header of a workflow file being opened, or placeholder metadata when it cannot be read.

    A file opens whether or not its header reads, so a header problem never blocks opening it.
    """
    try:
        return OpenedFileMetadata(metadata=read_workflow_metadata(file_path), metadata_is_placeholder=False)
    except WorkflowMetadataError as err:
        logger.debug("Opening '%s' without its metadata header: %s", file_path, err)
        placeholder = WorkflowMetadata(
            name=file_path.stem,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="",
            node_libraries_referenced=[],
        )
        return OpenedFileMetadata(metadata=placeholder, metadata_is_placeholder=True)
