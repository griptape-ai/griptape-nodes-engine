from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.files.path_utils import canonicalize_for_identity, derive_registry_key
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
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

if TYPE_CHECKING:
    from types import TracebackType

    from griptape_nodes.exe_types.core_types import BaseNodeElement
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

logger = logging.getLogger("griptape_nodes")


class ContextManager(EngineScoped):
    """Context manager for Workflow, Flow, Node, and Element contexts.

    Workflows own Flows, Flows own Nodes, and Nodes own Elements.
    There must always be a Workflow context active.
    Clients can push/pop Workflow contexts, Flow contexts, Node contexts, and Element contexts.
    """

    _workflow_stack: list[ContextManager.WorkflowContextState]
    _in_flight_workflow_load: WorkflowManager.WorkflowLoadRecord | None

    class WorkflowContextError(Exception):
        """Base exception for workflow context errors."""

    class NoActiveWorkflowError(WorkflowContextError):
        """No active workflow context error."""

    class NoActiveFlowError(WorkflowContextError):
        """No active flow context error."""

    class EmptyStackError(WorkflowContextError):
        """Empty stack error."""

    class WorkflowContextState:
        """Internal class that represents a Workflow's state which owns a stack of flow names."""

        _name: str
        _file_path: str | None
        _working_directory: str | None
        _load: WorkflowManager.WorkflowLoadRecord | None
        _flow_stack: list[ContextManager.FlowContextState]

        def __init__(
            self,
            name: str,
            file_path: str | None = None,
            working_directory: str | None = None,
            load: WorkflowManager.WorkflowLoadRecord | None = None,
        ):
            self._name = name
            # The path this context was entered WITH, when it was entered by path. Retained
            # because `_name` is a registry key derived against the workspace that was active
            # at push time, so it goes stale the moment the workspace changes -- a project
            # switch re-registers workflows under the new workspace and the lookup then misses.
            # Callers that want the workflow's location (see ProjectManager's `workflow_dir`
            # builtin) read this instead of round-tripping through WorkflowRegistry.
            self._file_path = file_path
            # The folder this workflow belongs to while it has no file of its own: the folder the
            # user was browsing when they created it. A DIRECTORY, unlike `_file_path`, which is
            # a file whose PARENT is the directory. Always loses to `_file_path` -- once the
            # workflow has been saved, the saved file's own location is the better answer.
            self._working_directory = working_directory
            # The load that is populating this context, if it was entered by one. See
            # `is_loading`.
            self._load = load
            self._flow_stack = []

        def is_loading(self) -> bool:
            """Whether a load is still populating this Workflow context.

            A workflow is entered before its nodes exist, so a current name is not proof it's usable.
            """
            return self._load is not None and self._load.in_progress

        def push_flow(self, flow: ControlFlow) -> ControlFlow:
            """Push a flow name onto this workflow's flow stack."""
            flow_context = ContextManager.FlowContextState(flow)
            self._flow_stack.append(flow_context)
            return flow

        def pop_flow(self) -> ControlFlow:
            """Pop the top flow from this workflow's flow stack."""
            if not self._flow_stack:
                msg = f"Cannot pop Flow: no active Flows in Workflow '{self._name}'"
                raise ContextManager.EmptyStackError(msg)

            flow_context = self._flow_stack.pop()
            return flow_context._flow

        def has_current_flow(self) -> bool:
            """Check if this workflow has an active flow."""
            return len(self._flow_stack) > 0

        def get_current_flow(self) -> ControlFlow:
            """Get the current flow in this workflow."""
            if not self._flow_stack:
                msg = f"No active Flow in Workflow '{self._name}'"
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
        self._in_flight_workflow_load = None
        event_manager.assign_manager_to_request_type(
            request_type=SetWorkflowContextRequest, callback=self.on_set_workflow_context_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=GetWorkflowContextRequest, callback=self.on_get_workflow_context_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=EnsureWorkflowAndFlowRequest, callback=self.on_ensure_workflow_and_flow_request
        )

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

        # When no workflow_name is supplied, mint a fresh "unsaved:<uuid>" key here so the
        # engine owns the namespace. Callers doing "create a new workflow" should omit the
        # name and read the resolved key off the success result.
        resolved_name = request.workflow_name or f"{WorkflowRegistry.UNSAVED_KEY_PREFIX}{uuid.uuid4()}"

        # Auto-register an unsaved registry entry when the caller is activating an
        # "unsaved:<uuid>" key. This makes every workflow (saved or not) a first-class
        # registry entry, so list/metadata/etc. calls don't need special-casing for
        # pre-save state. `ensure_unsaved` is idempotent.
        if resolved_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX):
            try:
                WorkflowRegistry.ensure_unsaved(key=resolved_name, display_name=request.display_name or "Untitled")
            except ValueError as err:
                msg = (
                    f"Attempted to auto-register unsaved workflow '{resolved_name}' "
                    f"before setting it as the Current Context. Failed because of '{err}'."
                )
                return SetWorkflowContextFailure(result_details=msg)

        self.push_workflow(resolved_name, working_directory=working_directory)
        msg = f"Successfully set the Workflow '{resolved_name}' as the Current Context."
        return SetWorkflowContextSuccess(workflow_name=resolved_name, result_details=msg)

    def on_get_workflow_context_request(self, request: GetWorkflowContextRequest) -> ResultPayload:  # noqa: ARG002
        workflow_name = None
        is_saved = None
        is_loading = False
        if self.has_current_workflow():
            workflow_name = self.get_current_workflow_name()
            is_loading = self.is_current_workflow_loading()
            if WorkflowRegistry.has_workflow_with_name(workflow_name):
                is_saved = WorkflowRegistry.get_workflow_by_name(workflow_name).is_saved
        return GetWorkflowContextSuccess(
            workflow_name=workflow_name,
            is_saved=is_saved,
            is_loading=is_loading,
            result_details=f"Successfully retrieved workflow context: {workflow_name or 'None'}",
        )

    def on_ensure_workflow_and_flow_request(self, request: EnsureWorkflowAndFlowRequest) -> ResultPayload:
        """Cold-start bootstrap that guarantees a workflow + flow context exist.

        Delegates the workflow side to SetWorkflowContextRequest so the unsaved-registry-key
        scheme ("unsaved:<uuid>") stays the single source of truth for scratch workflows;
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
                    logger.error(msg)
                    raise ValueError(msg)
                flow = control_flow
            except KeyError as e:
                msg = f"Flow '{flow}' not found in current workflow."
                logger.error(msg)
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
                logger.error(msg)
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
                    logger.error(msg)
                    raise ValueError(msg)
                element = node_element
            except KeyError as e:
                msg = f"Element '{element}' not found in current node."
                logger.error(msg)
                raise ValueError(msg) from e
        return self.ElementContext(self, element)

    def has_current_workflow(self) -> bool:
        """Check if there is an active Workflow context."""
        return len(self._workflow_stack) > 0

    def has_current_flow(self) -> bool:
        """Check if there is an active Flow context within the current Workflow."""
        if not self.has_current_workflow():
            return False

        current_workflow = self._workflow_stack[-1]
        return current_workflow.has_current_flow()

    def has_current_node(self) -> bool:
        """Check if there is an active Node within the current Flow."""
        if not self.has_current_flow():
            return False

        current_workflow = self._workflow_stack[-1]
        current_flow = current_workflow._flow_stack[-1]
        return current_flow.has_current_node()

    def has_current_element(self) -> bool:
        """Check if there is an active Element within the current Node."""
        if not self.has_current_node():
            return False

        current_workflow = self._workflow_stack[-1]
        current_flow = current_workflow._flow_stack[-1]
        current_node = current_flow._node_stack[-1]
        return current_node.has_current_element()

    def get_current_workflow_name(self) -> str:
        """Get the name of the current Workflow context.

        Returns:
            The name of the current Workflow.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        current_workflow = self._workflow_stack[-1]
        return current_workflow._name

    def get_current_workflow_file_path(self) -> str | None:
        """Get the file path the current Workflow context was entered with, if any.

        Returns the path this context was entered with: either the one passed to
        `push_workflow(file_path=...)`, or the one resolved from the registry at push time
        when entered by name. Unlike `get_current_workflow_name()`, this does not depend on
        the active workspace: the name is a registry key derived against the workspace at
        push time, so switching projects re-registers workflows under a different key and
        leaves the name stale. Prefer this when you need the workflow's LOCATION.

        Returns:
            The absolute file path, or None when entered by name for a workflow that was
            unregistered or unsaved at push time.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return self._workflow_stack[-1]._file_path

    def get_current_workflow_working_directory(self) -> str | None:
        """Get the folder the current Workflow context belongs to, if one was supplied.

        This is the folder a workflow was created in before it had a file of its own -- the
        folder the caller was browsing at the time. It is a DIRECTORY, whereas
        `get_current_workflow_file_path` returns a FILE whose parent is the directory.

        Only meaningful while the workflow is unsaved: `workflow_dir` prefers the retained
        file path whenever there is one, so this stops mattering the moment the workflow is
        saved. It is deliberately not cleared on save -- the file path simply wins.

        Returns:
            The absolute directory path, or None when no folder was supplied.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return self._workflow_stack[-1]._working_directory

    def is_current_workflow_loading(self) -> bool:
        """Whether a load is still populating the current Workflow context.

        Answers about the workflow this context IS, not whatever the engine happens to be
        executing: importing a referenced sub flow into an already-open workflow reads False.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        return self._workflow_stack[-1].is_loading()

    def set_current_workflow_name(self, new_name: str) -> None:
        """Update the name of the current Workflow context.

        Args:
            new_name: The new name to assign to the current Workflow.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        self._workflow_stack[-1]._name = new_name

    def set_current_workflow_file_path(self, new_file_path: str | None) -> None:
        """Update the file path retained on the current Workflow context.

        Anything that relocates the current workflow's file on disk (Move, Rename) must call
        this alongside `set_current_workflow_name`. The retained path is the authoritative
        answer for the workflow's location -- `get_current_workflow_file_path` is preferred
        over a registry lookup precisely because it survives a workspace switch -- so leaving
        it at the pre-move value keeps `workflow_dir` pointing at the old directory even
        though the registry is correct.

        Args:
            new_file_path: The workflow's new path, or None when it no longer has one.

        Raises:
            NoActiveWorkflowError: If no Workflow context is active.
        """
        if not self.has_current_workflow():
            msg = "No active Workflow context"
            raise self.NoActiveWorkflowError(msg)

        self._workflow_stack[-1]._file_path = new_file_path

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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
        current_flow = current_workflow._flow_stack[-1]

        if not current_flow.has_current_node():
            msg = "No active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.get_current_element()

    def get_in_flight_workflow_load(self) -> WorkflowManager.WorkflowLoadRecord | None:
        """The load currently populating context, if any. See `begin_workflow_load`."""
        return self._in_flight_workflow_load

    def begin_workflow_load(self, load: WorkflowManager.WorkflowLoadRecord) -> None:
        """Mark `load` as the load populating context, until the matching `end_workflow_load`.

        Stamps the current workflow (if any) as well as every workflow `push_workflow` enters from
        here on, so a load that rebuilds an already-open workflow in place is reported too.

        Called by WorkflowManager, which owns loads. See `WorkflowManager._tracking_workflow_load`.
        """
        self._in_flight_workflow_load = load
        if self.has_current_workflow():
            self._workflow_stack[-1]._load = load

    def end_workflow_load(self) -> None:
        """Stop stamping newly entered workflows with the in-flight load.

        Does not settle the workflows already stamped with it; they read the load's own
        `in_progress`, which its owner clears, so they all settle together.
        """
        self._in_flight_workflow_load = None

    def push_workflow(
        self,
        workflow_name: str | None = None,
        *,
        file_path: str | None = None,
        working_directory: str | None = None,
    ) -> str:
        """Push a new Workflow context onto the stack.

        The workflow's file path is captured here, while the registry key is still valid, and
        retained on the context (see `get_current_workflow_file_path`). `resolved_name` is a key
        derived against the CURRENT workspace, so it goes stale as soon as the workspace changes:
        switching projects re-registers every workflow under the new workspace, after which a
        lookup by the old key misses even though nothing about the file changed.

        Args:
            workflow_name: The name of the Workflow to enter. Use this when the registry key is already known.
            file_path: Path to the workflow file. The registry key will be derived from this path,
                using a workspace-relative path if possible. Mutually exclusive with workflow_name.
            working_directory: Folder the Workflow belongs to while it has no file of its own,
                for a workflow that has never been saved. A DIRECTORY, not a file path. Never
                affects the registry key, and never overrides `file_path`: a workflow that has
                a file answers with that file's own directory. Expected to be absolute --
                `on_set_workflow_context_request` normalizes before it reaches here.

        Returns:
            The name of the Workflow that was entered.

        Raises:
            ValueError: If neither or both of workflow_name and file_path are provided.
        """
        if workflow_name is not None and file_path is not None:
            msg = "Provide either workflow_name or file_path, not both."
            raise ValueError(msg)
        if workflow_name is not None:
            resolved_name = workflow_name
            # Entered by key, so resolve the path NOW rather than at read time: the key is
            # only guaranteed to resolve against the workspace that is active right now.
            # Best-effort -- an unregistered or unsaved workflow simply has no path, which
            # callers already handle.
            if file_path is None:
                try:
                    workflow = WorkflowRegistry.get_workflow_by_name(resolved_name)
                except KeyError:
                    file_path = None
                else:
                    if workflow.file_path is not None:
                        file_path = WorkflowRegistry.get_complete_file_path(workflow.file_path)
        elif file_path is not None:
            resolved = canonicalize_for_identity(file_path)
            workspace_path = canonicalize_for_identity(self.engine.config_manager.workspace_path)
            if resolved.is_relative_to(workspace_path):
                path_for_key = str(resolved.relative_to(workspace_path))
            else:
                path_for_key = str(resolved)
            resolved_name = derive_registry_key(path_for_key)
        else:
            msg = "Either workflow_name or file_path must be provided."
            raise ValueError(msg)

        workflow_context_state = self.WorkflowContextState(
            resolved_name,
            file_path=file_path,
            working_directory=working_directory,
            load=self._in_flight_workflow_load,
        )
        self._workflow_stack.append(workflow_context_state)
        return resolved_name

    def pop_workflow(self) -> str:
        """Pop the top Workflow from the stack.

        Returns:
            The name of the Workflow that was popped.

        Raises:
            EmptyStackError: If there are no active Workflows.
        """
        if not self._workflow_stack:
            msg = "Cannot pop Workflow: no active Workflows"
            raise self.EmptyStackError(msg)

        workflow_context = self._workflow_stack.pop()
        return workflow_context._name

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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
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

        current_workflow = self._workflow_stack[-1]
        current_flow = current_workflow._flow_stack[-1]

        if not current_flow.has_current_node():
            msg = "Cannot pop Element: no active Node context"
            raise self.EmptyStackError(msg)

        current_node = current_flow._node_stack[-1]
        return current_node.pop_element()
