from __future__ import annotations

import logging
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowMetadataError,
    WorkflowRegistryError,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultFailure,
    EnsureWorkflowAndFlowResultSuccess,
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    SetWorkflowContextFailure,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from types import TracebackType

    from griptape_nodes.exe_types.core_types import BaseNodeElement
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ContextManager(EngineScoped):
    """Context manager for Workflow, Flow, Node, and Element contexts.

    Workflows own Flows, Flows own Nodes, and Nodes own Elements.
    The Workflow context is the top of the context stack. A worker pushes none: while it runs a
    node, the Workflow context is the one it keeps for the runtime of the node's orchestrator
    workflow (`adopt_orchestrator_workflow`).
    Clients can push/pop Workflow contexts, Flow contexts, Node contexts, and Element contexts.
    """

    _workflow_stack: list[ContextManager.WorkflowContextState]
    # Weak, so a context goes with the worker runtime it belongs to.
    _worker_contexts: weakref.WeakKeyDictionary[WorkflowRuntime, ContextManager.WorkflowContextState]

    class WorkflowContextError(Exception):
        """Base exception for workflow context errors."""

    class NoActiveWorkflowError(WorkflowContextError):
        """No active workflow context error."""

    class NoActiveFlowError(WorkflowContextError):
        """No active flow context error."""

    class EmptyStackError(WorkflowContextError):
        """Empty stack error."""

    class OrchestratorWorkflowError(WorkflowContextError):
        """A worker could not adopt the workflow its orchestrator ran a node for."""

    class WorkflowContextState:
        """Internal class that represents a Workflow's state which owns a stack of flow names.

        Holds only the workflow's id. Its name, file and folder live on its registry entry, so a
        save, rename, or move updates them in one place and the context never goes stale.
        """

        workflow_id: str
        _flow_stack: list[ContextManager.FlowContextState]

        def __init__(self, workflow_id: str):
            self.workflow_id = workflow_id
            self._flow_stack = []

        def push_flow(self, flow: ControlFlow) -> ControlFlow:
            """Push a flow name onto this workflow's flow stack."""
            flow_context = ContextManager.FlowContextState(flow)
            self._flow_stack.append(flow_context)
            return flow

        def pop_flow(self) -> ControlFlow:
            """Pop the top flow from this workflow's flow stack."""
            if not self._flow_stack:
                msg = f"Cannot pop Flow: no active Flows in Workflow '{self.workflow_id}'"
                raise ContextManager.EmptyStackError(msg)

            flow_context = self._flow_stack.pop()
            return flow_context._flow

        def has_current_flow(self) -> bool:
            """Check if this workflow has an active flow."""
            return len(self._flow_stack) > 0

        def get_current_flow(self) -> ControlFlow:
            """Get the current flow in this workflow."""
            if not self._flow_stack:
                msg = f"No active Flow in Workflow '{self.workflow_id}'"
                raise ContextManager.EmptyStackError(msg)

            flow_context = self._flow_stack[-1]
            return flow_context._flow

    class WorkflowContext:
        """A context manager for a Workflow."""

        _manager: ContextManager
        _workflow_name: str

        def __init__(self, manager: ContextManager, workflow_name: str):
            self._manager = manager
            self._workflow_name = workflow_name

        def __enter__(self) -> None:
            self._manager.push_workflow(self._workflow_name)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self._manager.pop_workflow()

    class FlowContextState:
        """Internal class that represents a Flow's state which owns a stack of node names."""

        _flow: ControlFlow
        _node_stack: list[ContextManager.NodeContextState]

        def __init__(self, flow: ControlFlow):
            self._flow = flow
            self._node_stack = []

        def push_node(self, node: BaseNode) -> BaseNode:
            """Push a node name onto this flow's node stack."""
            node_context = ContextManager.NodeContextState(node)
            self._node_stack.append(node_context)
            return node

        def pop_node(self) -> BaseNode:
            """Pop the top node from this flow's node stack."""
            if not self._node_stack:
                msg = f"Cannot pop Node: no active Nodes in Flow '{self._flow.name}'"
                raise ContextManager.EmptyStackError(msg)

            node_context = self._node_stack.pop()
            return node_context.node

        def get_current_node(self) -> BaseNode:
            """Get the name of the current node in this flow."""
            if not self._node_stack:
                msg = f"No active Node in Flow '{self._flow.name}'"
                raise ContextManager.EmptyStackError(msg)

            node_context = self._node_stack[-1]
            return node_context.node

        def has_current_node(self) -> bool:
            """Check if this flow has an active node."""
            return len(self._node_stack) > 0

    class NodeContextState:
        """Internal class that represents a Node's state which owns a stack of node elements."""

        node: BaseNode
        _element_stack: list[BaseNodeElement]

        def __init__(self, node: BaseNode):
            self.node = node
            self._element_stack = []

        def push_element(self, element: BaseNodeElement) -> BaseNodeElement:
            """Push an element name onto this node's element stack."""
            self._element_stack.append(element)
            return element

        def pop_element(self) -> BaseNodeElement:
            """Pop the top element from this node's element stack."""
            if not self._element_stack:
                msg = f"Cannot pop Element: no active Elements in Node '{self.node.name}'"
                raise ContextManager.EmptyStackError(msg)

            element = self._element_stack.pop()
            return element

        def get_current_element(self) -> BaseNodeElement:
            """Get the name of the current element in this node."""
            if not self._element_stack:
                msg = f"No active Element in Node '{self.node.name}'"
                raise ContextManager.EmptyStackError(msg)

            return self._element_stack[-1]

        def has_current_element(self) -> bool:
            """Check if this node has an active element."""
            return len(self._element_stack) > 0

    # The admittedly-confusing term for using these as a Python context (e.g., the `with` keyword)
    class FlowContext:
        """A context manager for a Flow."""

        _manager: ContextManager
        _flow: ControlFlow

        def __init__(self, manager: ContextManager, flow: ControlFlow):
            self._manager = manager
            self._flow = flow

        def __enter__(self) -> ControlFlow:
            return self._manager.push_flow(self._flow)

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self._manager.pop_flow()

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
        """Initialize the context manager with empty workflow and flow stacks."""
        super().__init__(engine)
        self._workflow_stack = []
        self._worker_contexts = weakref.WeakKeyDictionary()
        event_manager.register_request_handlers(self)

    @handles(SetWorkflowContextRequest)
    def on_set_workflow_context_request(self, request: SetWorkflowContextRequest) -> ResultPayload:
        # As of today, we only allow a single Workflow context at a time. This may change in the future.
        if self.has_current_workflow():
            msg = f"Attempted to set the Workflow '{request.workflow_name}' as the Current Context. Failed because an existing workflow, '{self.get_current_workflow_name()}', is already in the Current Context. In order to clear the existing workflow and remove all objects and references to it, issue a ClearAllObjectState request."
            return SetWorkflowContextFailure(result_details=msg)

        # Normalized here rather than at read time so `workflow_dir` never has to care whether
        # the caller sent an absolute path, a workspace-relative one, or one with a `~` in it.
        working_directory = None
        if request.working_directory is not None:
            working_directory = str(
                canonicalize_for_identity(request.working_directory, base=self.engine.config_manager.workspace_path)
            )
            # A file where a folder was meant is the one mistake worth rejecting: the value
            # becomes the parent of every path the workflow writes, so accepting it would put
            # outputs beside the file rather than in the folder the caller named -- a plausible
            # location, which is what makes it hard to notice.
            existing = Path(working_directory)
            if existing.exists() and not existing.is_dir():
                msg = (
                    f"Attempted to set the folder for a new Workflow to '{request.working_directory}'. "
                    f"Failed because that path is a file, not a folder."
                )
                return SetWorkflowContextFailure(result_details=msg)

        # When no workflow_name is supplied, register a new unsaved workflow here so the engine
        # owns the namespace. Callers doing "create a new workflow" should omit the name and
        # read the resolved key off the success result.
        registry = self.engine.workflow_registry
        display_name = request.display_name or "Untitled"
        workflow_name = request.workflow_name
        if workflow_name is None:
            workflow_name = registry.register_unsaved(display_name).name
        elif registry.find_by_name(workflow_name) is None:
            # A name no workflow has fails, rather than opening a blank workflow under it: a typo,
            # a name another client just renamed, or a file the workspace scan has not registered
            # yet would otherwise show an empty canvas whose next save writes a new file. The
            # handler does not wait for the scan: it is synchronous, and the scan's completion
            # event is already set before the first scan starts, so waiting would not cover an
            # editor that reconnects that early. It fails instead, and the editor can ask again.
            # Only an "unsaved:<id>" name, which an editor sends back to restore an unsaved
            # workflow, registers a new unsaved entry under that id. This makes every workflow
            # (saved or not) a first-class registry entry, so list/metadata/etc. calls don't
            # need special-casing for pre-save state.
            workflow_id = registry.unsaved_name_workflow_id(workflow_name)
            if workflow_id is None:
                msg = (
                    f"Attempted to open workflow '{workflow_name}'. Failed because no workflow with that name "
                    f"is registered. Open a workflow from the workflow list, or start a new one."
                )
                return SetWorkflowContextFailure(result_details=msg)
            try:
                workflow_name = registry.register_unsaved(display_name, workflow_id=workflow_id).name
            except WorkflowRegistryError as err:
                msg = (
                    f"Attempted to auto-register unsaved workflow '{workflow_name}' "
                    f"before setting it as the Current Context. Failed because of '{err}'."
                )
                return SetWorkflowContextFailure(result_details=msg)

        resolved_name = self.push_workflow(workflow_name, working_directory=working_directory)
        msg = f"Successfully set the Workflow '{resolved_name}' as the Current Context."
        return SetWorkflowContextSuccess(
            workflow_name=resolved_name, workflow_id=self.get_current_workflow_id(), result_details=msg
        )

    @handles(GetWorkflowContextRequest)
    def on_get_workflow_context_request(self, request: GetWorkflowContextRequest) -> ResultPayload:  # noqa: ARG002
        workflow_name = None
        workflow_id = None
        is_saved = None
        if self.has_current_workflow():
            workflow = self.get_current_workflow()
            workflow_name = workflow.name
            workflow_id = workflow.workflow_id
            is_saved = workflow.is_saved
        return GetWorkflowContextSuccess(
            workflow_name=workflow_name,
            workflow_id=workflow_id,
            is_saved=is_saved,
            result_details=f"Successfully retrieved workflow context: {workflow_name or 'None'}",
        )

    @handles(EnsureWorkflowAndFlowRequest)
    def on_ensure_workflow_and_flow_request(self, request: EnsureWorkflowAndFlowRequest) -> ResultPayload:
        """Cold-start bootstrap that guarantees a workflow + flow context exist.

        Delegates the workflow side to SetWorkflowContextRequest so the unsaved-registry-key
        scheme ("unsaved:<workflow id>") stays the single source of truth for scratch workflows;
        composes that with CreateFlowRequest so callers can go from a blank engine to
        CreateNode in a single round trip.
        """
        # Lazy import required: context_manager is imported by griptape_nodes, creating a circular dependency.
        from griptape_nodes.retained_mode.events.flow_events import (
            CreateFlowRequest,
            CreateFlowResultSuccess,
        )

        created_workflow = False
        if self.has_current_workflow():
            workflow_name = self.get_current_workflow_name()
            workflow_id = self.get_current_workflow_id()
        else:
            set_workflow_result = self.engine.handle_request(
                SetWorkflowContextRequest(
                    workflow_name=request.workflow_name,
                    display_name=request.display_name,
                    working_directory=request.working_directory,
                )
            )
            if not isinstance(set_workflow_result, SetWorkflowContextSuccess):
                return EnsureWorkflowAndFlowResultFailure(
                    result_details=(
                        f"Attempted to ensure a workflow + flow context. Failed while setting the workflow: "
                        f"{set_workflow_result.result_details}"
                    )
                )
            workflow_name = set_workflow_result.workflow_name
            workflow_id = set_workflow_result.workflow_id
            created_workflow = True

        created_flow = False
        if self.has_current_flow():
            flow_name = self.get_current_flow().name
        else:
            create_flow_result = self.engine.handle_request(
                CreateFlowRequest(
                    parent_flow_name=None,
                    flow_name=request.flow_name,
                    set_as_new_context=True,
                )
            )
            if not isinstance(create_flow_result, CreateFlowResultSuccess):
                # Roll the workflow push back so a partial failure does not leave hanging state.
                if created_workflow:
                    self.pop_workflow()
                return EnsureWorkflowAndFlowResultFailure(
                    result_details=(
                        f"Attempted to ensure a workflow + flow context. Failed while creating the flow: "
                        f"{create_flow_result.result_details}"
                    )
                )
            flow_name = create_flow_result.flow_name
            created_flow = True

        return EnsureWorkflowAndFlowResultSuccess(
            workflow_name=workflow_name,
            workflow_id=workflow_id,
            flow_name=flow_name,
            created_workflow=created_workflow,
            created_flow=created_flow,
            result_details=(
                f"Workflow '{workflow_name}' and Flow '{flow_name}' are ready "
                f"(created_workflow={created_workflow}, created_flow={created_flow})."
            ),
        )

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
        workflow = self.engine.worker_workflows.runtime_for(workflow_id)
        if workflow not in self._worker_contexts:
            self._worker_contexts[workflow] = self.WorkflowContextState(workflow_id)
        return workflow

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
        """Check if there is an active Workflow context: the top of the stack, or a worker's for its current runtime."""
        return self._current_state_or_none() is not None

    def has_current_flow(self) -> bool:
        """Check if there is an active Flow context within the current Workflow."""
        if not self.has_current_workflow():
            return False

        current_workflow = self._current_state()
        return current_workflow.has_current_flow()

    def has_current_node(self) -> bool:
        """Check if there is an active Node within the current Flow."""
        if not self.has_current_flow():
            return False

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]
        return current_flow.has_current_node()

    def has_current_element(self) -> bool:
        """Check if there is an active Element within the current Node."""
        if not self.has_current_node():
            return False

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]
        current_node = current_flow._node_stack[-1]
        return current_node.has_current_element()

    def is_workflow_in_context(self, workflow_id: str) -> bool:
        """Whether the workflow with this id is anywhere on the context stack."""
        return any(state.workflow_id == workflow_id for state in self._workflow_stack)

    def get_current_workflow_id(self) -> str:
        """Get the id of the current Workflow context.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return self._current_state().workflow_id

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

    def get_current_workflow_file_path(self) -> str | None:
        """Get the absolute path of the current Workflow's file, if it has one.

        Read from the workflow's registry entry, which saves, renames and moves keep current.

        Returns:
            The absolute file path, or None for an unsaved workflow.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        absolute_path = self.get_current_workflow().absolute_path
        if absolute_path is None:
            return None
        return str(absolute_path)

    def get_current_workflow_working_directory(self) -> str | None:
        """Get the folder the current Workflow belongs to, if one was supplied.

        This is the folder a workflow was created in before it had a file of its own -- the
        folder the caller was browsing at the time. It is a DIRECTORY, whereas
        `get_current_workflow_file_path` returns a FILE whose parent is the directory.

        Only meaningful while the workflow is unsaved: `workflow_dir` prefers the file path
        whenever there is one, so this stops mattering the moment the workflow is saved. It is
        deliberately not cleared on save -- the file path simply wins.

        Returns:
            The absolute directory path, or None when no folder was supplied.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        return self.get_current_workflow().working_directory

    def move_current_workflow_onto(self, workflow_id: str) -> None:
        """Make the registry entry `workflow_id` the current Workflow, keeping the open flows.

        For the one case where an open workflow becomes a different registry entry: saving an
        open library workflow writes a copy, and the editor keeps working in the copy.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        moved = self._workflow_stack[-1]
        open_workflows = self.engine.open_workflows
        # The open workflow's runtime follows it, so a request naming the copy's id reaches it.
        if len(self._workflow_stack) == 1 and open_workflows.is_open(moved.workflow_id):
            open_workflows.move(open_workflows.get(moved.workflow_id), workflow_id)
        moved.workflow_id = workflow_id

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

        current_workflow = self._current_state()
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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]
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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]

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
        """Push a new Workflow context onto the stack.

        The context holds only the workflow's id; its name, file and folder are read from its
        registry entry.

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
            The name of the Workflow that was entered.

        Raises:
            ValueError: If neither or both of workflow_name and file_path are provided.
        """
        if workflow_name is not None and file_path is not None:
            msg = "Provide either workflow_name or file_path, not both."
            raise ValueError(msg)
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

        if working_directory is not None:
            workflow.working_directory = working_directory
        if not self._workflow_stack:
            # The workflow at the bottom of the stack is the open one. A push onto it opens nothing.
            self.engine.open_workflows.open(workflow.workflow_id)
        self._workflow_stack.append(self.WorkflowContextState(workflow.workflow_id))
        return workflow.name

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
        """Pop the top Workflow from the stack.

        Returns:
            The name of the Workflow that was popped. `get_current_workflow_id` gives its id
            beforehand.

        Raises:
            EmptyStackError: If there are no active Workflows.
        """
        if not self._workflow_stack:
            msg = "Cannot pop Workflow: no active Workflows"
            raise self.EmptyStackError(msg)

        name = self.get_current_workflow_name()
        popped = self._workflow_stack.pop()
        open_workflows = self.engine.open_workflows
        if not self._workflow_stack and open_workflows.is_open(popped.workflow_id):
            closed = open_workflows.get(popped.workflow_id)
            open_workflows.close(closed)
            self.engine.leave_closed_workflow(closed)
        return name

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

        current_workflow = self._current_state()
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

        current_workflow = self._current_state()
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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]
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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]
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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]

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

        current_workflow = self._current_state()
        current_flow = current_workflow._flow_stack[-1]

        if not current_flow.has_current_node():
            msg = "Cannot pop Element: no active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.pop_element()

    def _current_state(self) -> ContextManager.WorkflowContextState:
        """The current Workflow context. Callers check `has_current_workflow` first.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        state = self._current_state_or_none()
        if state is None:
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)
        return state

    def _current_state_or_none(self) -> ContextManager.WorkflowContextState | None:
        """The top of the context stack, or on a worker the context of its current runtime, or None."""
        if self._workflow_stack:
            return self._workflow_stack[-1]
        workflow = self.engine.current_workflow_or_none()
        if workflow is None:
            return None
        return self._worker_contexts.get(workflow)


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
