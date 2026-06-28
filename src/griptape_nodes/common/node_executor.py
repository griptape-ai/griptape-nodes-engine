from __future__ import annotations

import asyncio
import logging
import pickle
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import anyio

from griptape_nodes.bootstrap.workflow_publishers.subprocess_workflow_publisher import SubprocessWorkflowPublisher
from griptape_nodes.drivers.storage.storage_backend import StorageBackend
from griptape_nodes.exe_types import node_types
from griptape_nodes.exe_types.base_iterative_nodes import (
    BaseIterativeEndNode,
    BaseIterativeStartNode,
    IterativeNodeParam,
)
from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.exe_types.node_groups import (
    BaseIterativeNodeGroup,
    BaseWhileNodeGroup,
    IterationControlParam,
    SubflowNodeGroup,
    WhileControlParam,
)
from griptape_nodes.exe_types.node_types import (
    CONTROL_INPUT_PARAMETER,
    LOCAL_EXECUTION,
    PRIVATE_EXECUTION,
    BaseNode,
    EndNode,
    NodeResolutionState,
    StartNode,
)
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.machines.dag_builder import DagBuilder
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.agent_events import AgentStreamEvent
from griptape_nodes.retained_mode.events.base_events import ForwardedException, ProgressEvent
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionResultFailure,
    CreateConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    ControlFlowResolvedEvent,
    CurrentControlNodeEvent,
    CurrentDataNodeEvent,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
    GriptapeEvent,
    InvolvedNodesEvent,
    NodeFinishProcessEvent,
    NodeMetadata,
    NodeResolvedEvent,
    NodeStartProcessEvent,
    NodeUnresolvedEvent,
    ParameterSpotlightEvent,
    ParameterValueUpdateEvent,
    SelectedControlOutputEvent,
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowResultFailure,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    DeleteFlowResultFailure,
    DeleteFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultFailure,
    DeserializeFlowFromCommandsResultSuccess,
    PackagedNodeParameterMapping,
    PackageNodesAsSerializedFlowRequest,
    PackageNodesAsSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeserializeNodeFromCommandsResultFailure,
    DeserializeNodeFromCommandsResultSuccess,
    SetLockNodeStateResultFailure,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterElementEvent,
    RemoveElementEvent,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    DeleteWorkflowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    LoadWorkflowMetadata,
    LoadWorkflowMetadataResultSuccess,
    PublishWorkflowProgressEvent,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    SaveWorkflowFileFromSerializedFlowRequest,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.event_manager import (
    EventSuppressionContext,
    EventTranslationContext,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

logger = logging.getLogger("griptape_nodes")

# Tracks the name of the node currently being executed in this async context.
# Each asyncio task gets its own copy, so parallel node execution is safe.
current_executing_node_name: ContextVar[str | None] = ContextVar("current_executing_node_name", default=None)


class IterationControlAction(StrEnum):
    """Enum for iterative group control actions."""

    ADD = "add"  # Normal path - add result to list
    SKIP = "skip"  # Skip this iteration, don't add result
    BREAK = "break"  # Break out of loop immediately


LOOP_EVENTS_TO_SUPPRESS = {
    CreateFlowResultSuccess,
    CreateFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    DeserializeNodeFromCommandsResultSuccess,
    DeserializeNodeFromCommandsResultFailure,
    CreateConnectionResultSuccess,
    CreateConnectionResultFailure,
    SetParameterValueResultSuccess,
    SetParameterValueResultFailure,
    SetLockNodeStateResultSuccess,
    SetLockNodeStateResultFailure,
    DeserializeFlowFromCommandsResultSuccess,
    DeserializeFlowFromCommandsResultFailure,
}

EXECUTION_EVENTS_TO_SUPPRESS = {
    CurrentControlNodeEvent,
    CurrentDataNodeEvent,
    SelectedControlOutputEvent,
    ParameterSpotlightEvent,
    ControlFlowResolvedEvent,
    ControlFlowCancelledEvent,
    NodeResolvedEvent,
    ParameterValueUpdateEvent,
    NodeUnresolvedEvent,
    NodeStartProcessEvent,
    NodeFinishProcessEvent,
    InvolvedNodesEvent,
    GriptapeEvent,
    PublishWorkflowProgressEvent,
    AgentStreamEvent,
    AlterElementEvent,
    RemoveElementEvent,
    StartLocalSubflowResultSuccess,
    StartLocalSubflowResultFailure,
    ProgressEvent,
}


@dataclass
class PublishWorkflowStartEndNodes:
    start_flow_node_type: str
    start_flow_node_library_name: str
    end_flow_node_type: str
    end_flow_node_library_name: str


class PublishLocalWorkflowResult(NamedTuple):
    """Result from publishing a local workflow."""

    workflow_result: SaveWorkflowFileFromSerializedFlowResultSuccess
    file_name: str
    output_parameter_prefix: str
    package_result: PackageNodesAsSerializedFlowResultSuccess


class EntryNodeParameter(NamedTuple):
    """Entry node and Entry Parameter."""

    entry_node: str | None
    entry_parameter: str | None


class LoopBodyNodes(NamedTuple):
    """Result of collecting loop body nodes."""

    all_nodes: set[str]
    execution_type: str
    node_group_name: str | None


class NodeExecutor:
    """Singleton executor that executes nodes dynamically."""

    def get_workflow_handler(self, library_name: str) -> LibraryManager.RegisteredEventHandler:
        """Get the PublishWorkflowRequest handler for a library, or None if not available."""
        library_manager = GriptapeNodes.LibraryManager()
        registered_handlers = library_manager.get_registered_event_handlers(PublishWorkflowRequest)
        if library_name in registered_handlers:
            return registered_handlers[library_name]
        msg = f"Could not find PublishWorkflowRequest handler for library {library_name}"
        raise ValueError(msg)

    async def execute(self, node: BaseNode) -> None:
        """Execute the given node.

        Args:
            node: The BaseNode to execute
            library_name: The library that the execute method should come from.
        """
        token = current_executing_node_name.set(node.name)
        try:
            # Handle while-loop node groups (RetryGroup, etc.)
            # Check this BEFORE SubflowNodeGroup since BaseWhileNodeGroup extends SubflowNodeGroup
            if isinstance(node, BaseWhileNodeGroup):
                await self.handle_while_group_execution(node)
                return

            # Handle iterative node groups (ForEachGroup, ForLoopGroup, etc.)
            # Check this BEFORE SubflowNodeGroup since BaseIterativeNodeGroup extends SubflowNodeGroup
            if isinstance(node, BaseIterativeNodeGroup):
                await self.handle_iterative_group_execution(node)
                return

            if isinstance(node, SubflowNodeGroup):
                execution_type = node.get_parameter_value(node.execution_environment.name)
                if execution_type == LOCAL_EXECUTION:
                    # Just execute the node normally! This means we aren't doing any special packaging.
                    await node.aprocess()
                    return
                # Clear execution state before subprocess execution starts
                node.subflow_execution_component.clear_execution_state()
                if execution_type == PRIVATE_EXECUTION:
                    # Package the flow and run it in a subprocess.
                    await self._execute_private_workflow(node)
                    return
                # If it isn't Local or Private, it must be a library name. We'll try to execute it, and if the library name doesn't exist, it'll raise an error.
                await self._execute_library_workflow(node, execution_type)
                return

            # Handle iterative loop nodes - check if we need to package and execute the loop
            if isinstance(node, BaseIterativeEndNode):
                await self.handle_loop_execution(node)
                return

            # Single entry point for both local and worker execution. The
            # ExecuteNodeRequest handler routes to a worker subprocess when the
            # node's library requires it, otherwise runs aprocess in-process.
            result = await GriptapeNodes.ahandle_request(
                ExecuteNodeRequest(
                    node_name=node.name,
                    parameter_values=dict(node.parameter_values),
                    node_metadata=cast("NodeMetadata", dict(node.metadata)),
                )
            )
            if not isinstance(result, ExecuteNodeResultSuccess):
                exc = getattr(result, "exception", None)
                msg = self._format_node_failure_message(node.name, result, exc)
                raise RuntimeError(msg) from exc  # noqa: TRY004
            # Copy outputs back onto the in-memory node. Write directly into
            # parameter_output_values (not through set_parameter_value, which
            # targets parameter_values and re-fires before/after_value_set and
            # lifecycle events). TrackedParameterOutputValues.__setitem__
            # guards with old_value != value, so on the local/in-process path
            # -- where aprocess already wrote these entries in place -- the
            # write is idempotent and emits no duplicate AlterElementEvent.
            # Downstream delivery is handled later by
            # parallel_resolution.collect_values_from_upstream_nodes, which
            # reads from parameter_output_values.
            for name, value in result.parameter_output_values.items():
                node.parameter_output_values[name] = value
        finally:
            current_executing_node_name.reset(token)

    @staticmethod
    def _format_node_failure_message(node_name: str, result: Any, exc: BaseException | None) -> str:
        """Compose the RuntimeError message for a failed node execution.

        When the worker rebuilt a ``ForwardedException``, surface its
        ``original_type`` and ``original_traceback`` directly in the
        message. Chaining via ``from exc`` is not enough on its own:
        a ForwardedException is constructed (not raised) on the
        receiving side, so its ``__traceback__`` is None and Python's
        chained-exception display prints only the cause's
        ``Type: message`` line with no frames. Interpolating
        ``original_traceback`` here is what actually puts the worker
        frames in front of the user.
        """
        type_prefix = ""
        tb_suffix = ""
        if isinstance(exc, ForwardedException):
            if exc.original_type:
                type_prefix = f"[{exc.original_type}] "
            if exc.original_traceback:
                tb_suffix = f"\nWorker traceback:\n{exc.original_traceback}"
        return (
            f"Node '{node_name}' execution failed: {type_prefix}{getattr(result, 'result_details', result)}{tb_suffix}"
        )

    async def _execute_and_apply_workflow(
        self,
        node: BaseNode,
        workflow_path: Path,
        file_name: str,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> None:
        """Execute workflow in subprocess and apply results to node.

        Args:
            node: The node to apply results to
            workflow_path: Path to workflow file to execute
            file_name: Name of workflow for logging
            package_result: The packaging result containing parameter mappings
        """
        # Pass node for event updates if it's a SubflowNodeGroup
        subflow_node = node if isinstance(node, SubflowNodeGroup) else None
        my_subprocess_result = await self._execute_subprocess(workflow_path, file_name, node=subflow_node)
        parameter_output_values = self._extract_parameter_output_values(my_subprocess_result)
        self._apply_parameter_values_to_node(node, parameter_output_values, package_result)

    async def _execute_private_workflow(self, node: BaseNode) -> None:
        """Execute node in private subprocess environment.

        Args:
            node: The node to execute
        """
        workflow_result = None
        try:
            result = await self._publish_local_workflow(node)
            if result is None:
                # Length of list is 0, no node names in group.
                return
            workflow_result = result.workflow_result
        except Exception as e:
            logger.exception(
                "Failed to publish local workflow for node '%s'. Node type: %s",
                node.name,
                node.__class__.__name__,
            )
            msg = f"Failed to publish workflow for node '{node.name}': {e}"
            raise RuntimeError(msg) from e

        try:
            await self._execute_and_apply_workflow(
                node=node,
                workflow_path=Path(workflow_result.file_path),
                file_name=result.file_name,
                package_result=result.package_result,
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.exception(
                "Subprocess execution failed for node '%s'. Node type: %s",
                node.name,
                node.__class__.__name__,
            )
            msg = f"Failed to execute node '{node.name}' in local subprocess: {e}"
            raise RuntimeError(msg) from e
        finally:
            if workflow_result is not None:
                await self._delete_workflow(workflow_path=Path(workflow_result.file_path))

    async def _execute_library_workflow(self, node: BaseNode, execution_type: str) -> None:
        """Execute node via library handler.

        Args:
            node: The node to execute
            execution_type: Library name for execution
        """
        try:
            library = LibraryRegistry.get_library(name=execution_type)
        except KeyError:
            msg = f"Could not find library for execution environment {execution_type} for node {node.name}."
            raise RuntimeError(msg)  # noqa: B904

        library_name = library.get_library_data().name

        try:
            self.get_workflow_handler(library_name)
        except ValueError as e:
            logger.error("Library execution failed for node '%s' via library '%s': %s", node.name, library_name, e)
            msg = f"Failed to execute node '{node.name}' via library '{library_name}': {e}"
            raise RuntimeError(msg) from e

        workflow_result = None
        published_workflow_filename = None

        try:
            result = await self._publish_local_workflow(node, library=library)
            if result is None:
                # Length of list is 0, no node names in group.
                return
            workflow_result = result.workflow_result
        except Exception as e:
            logger.exception(
                "Failed to publish local workflow for node '%s' via library '%s'. Node type: %s",
                node.name,
                library_name,
                node.__class__.__name__,
            )
            msg = f"Failed to publish workflow for node '{node.name}' via library '{library_name}': {e}"
            raise RuntimeError(msg) from e

        try:
            published_workflow_filename = await self._publish_library_workflow(
                workflow_result, library_name, result.file_name, node=node
            )
        except Exception as e:
            logger.exception(
                "Failed to publish library workflow for node '%s' via library '%s'. Node type: %s",
                node.name,
                library_name,
                node.__class__.__name__,
            )
            msg = f"Failed to publish library workflow for node '{node.name}' via library '{library_name}': {e}"
            raise RuntimeError(msg) from e

        try:
            await self._execute_and_apply_workflow(
                node,
                published_workflow_filename,
                result.file_name,
                result.package_result,
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.exception(
                "Subprocess execution failed for node '%s' via library '%s'. Node type: %s",
                node.name,
                library_name,
                node.__class__.__name__,
            )
            msg = f"Failed to execute node '{node.name}' via library '{library_name}': {e}"
            raise RuntimeError(msg) from e
        finally:
            if workflow_result is not None:
                await self._delete_workflow(workflow_path=Path(workflow_result.file_path))
            if published_workflow_filename is not None:
                await self._delete_workflow(workflow_path=published_workflow_filename)

    async def _get_workflow_start_end_nodes(self, library: Library | None) -> PublishWorkflowStartEndNodes:
        library_name = "Griptape Nodes Library"
        start_node_type = "StartFlow"
        end_node_type = "EndFlow"

        if library is not None:
            # Attempt to get start and end nodes from the registered handler
            library_name = library.get_library_data().name
            registered_event_handler = self.get_workflow_handler(library_name)
            registered_event_data = registered_event_handler.event_data
            if registered_event_data is not None and isinstance(
                registered_event_data, PublishWorkflowRegisteredEventData
            ):
                return PublishWorkflowStartEndNodes(
                    start_flow_node_type=registered_event_data.start_flow_node_type,
                    start_flow_node_library_name=registered_event_data.start_flow_node_library_name,
                    end_flow_node_type=registered_event_data.end_flow_node_type,
                    end_flow_node_library_name=registered_event_data.end_flow_node_library_name,
                )

            start_nodes = library.get_nodes_by_base_type(StartNode)
            end_nodes = library.get_nodes_by_base_type(EndNode)
            if len(start_nodes) > 0 and len(end_nodes) > 0:
                start_node_type = start_nodes[0]
                end_node_type = end_nodes[0]
                library_name = library.get_library_data().name

        return PublishWorkflowStartEndNodes(
            start_flow_node_type=start_node_type,
            start_flow_node_library_name=library_name,
            end_flow_node_type=end_node_type,
            end_flow_node_library_name=library_name,
        )

    async def _publish_local_workflow(
        self, node: BaseNode, library: Library | None = None
    ) -> PublishLocalWorkflowResult | None:
        """Package and publish a workflow for subprocess execution.

        Returns:
            PublishLocalWorkflowResult containing workflow_result, file_name, and output_parameter_prefix
        """
        sanitized_node_name = node.name.replace(" ", "_")
        output_parameter_prefix = f"{sanitized_node_name}_packaged_node_"
        # We have to make our defaults strings because the PackageNodesAsSerializedFlowRequest doesn't accept None types.
        library_name = library.get_library_data().name if library is not None else "Griptape Nodes Library"
        workflow_start_end_nodes = await self._get_workflow_start_end_nodes(library)

        sanitized_library_name = library_name.replace(" ", "_")
        # If we are packaging a SubflowNodeGroup, that means that we are packaging multiple nodes together, so we have to get the list of nodes from the group node.
        if isinstance(node, SubflowNodeGroup):
            node_names = list(node.get_all_nodes().keys())
        else:
            # Otherwise, it's a list of one node!
            node_names = [node.name]

        if len(node_names) == 0:
            return None

        # Pass node_group_name if we're packaging a SubflowNodeGroup
        node_group_name = node.name if isinstance(node, SubflowNodeGroup) else None

        request = PackageNodesAsSerializedFlowRequest(
            node_names=node_names,
            start_node_type=workflow_start_end_nodes.start_flow_node_type,
            end_node_type=workflow_start_end_nodes.end_flow_node_type,
            start_node_library_name=workflow_start_end_nodes.start_flow_node_library_name,
            end_node_library_name=workflow_start_end_nodes.end_flow_node_library_name,
            output_parameter_prefix=output_parameter_prefix,
            entry_control_node_name=None,
            entry_control_parameter_name=None,
            node_group_name=node_group_name,
        )
        package_result = GriptapeNodes.handle_request(request)
        if not isinstance(package_result, PackageNodesAsSerializedFlowResultSuccess):
            msg = f"Failed to package node '{node.name}'. Error: {package_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004

        file_name = f"{sanitized_node_name}_{sanitized_library_name}_packaged_flow"
        workflow_file_request = SaveWorkflowFileFromSerializedFlowRequest(
            file_name=file_name,
            serialized_flow_commands=package_result.serialized_flow_commands,
            workflow_shape=package_result.workflow_shape,
            pickle_control_flow_result=True,
        )

        workflow_result = await GriptapeNodes.ahandle_request(workflow_file_request)
        if not isinstance(workflow_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
            msg = f"Failed to Save Workflow File from Serialized Flow for node '{node.name}'. Error: {workflow_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004

        return PublishLocalWorkflowResult(
            workflow_result=workflow_result,
            file_name=file_name,
            output_parameter_prefix=output_parameter_prefix,
            package_result=package_result,
        )

    async def _publish_library_workflow(
        self,
        workflow_result: SaveWorkflowFileFromSerializedFlowResultSuccess,
        library_name: str,
        file_name: str,
        node: BaseNode | None = None,
    ) -> Path:
        # Define event callback if node is a SubflowNodeGroup for GUI updates
        on_event: Callable[[dict], None] | None = None
        if isinstance(node, SubflowNodeGroup):
            on_event = node.subflow_execution_component.handle_publishing_event

        subprocess_workflow_publisher = SubprocessWorkflowPublisher(on_event=on_event)
        published_filename = f"{Path(workflow_result.file_path).stem}_published"
        published_workflow_filename = GriptapeNodes.ConfigManager().workspace_path / (published_filename + ".py")

        async with subprocess_workflow_publisher:
            await subprocess_workflow_publisher.arun(
                workflow_name=file_name,
                workflow_path=workflow_result.file_path,
                publisher_name=library_name,
                published_workflow_file_name=published_filename,
                pickle_control_flow_result=True,
            )

        if not await anyio.Path(published_workflow_filename).exists():
            msg = f"Published workflow file does not exist at path: {published_workflow_filename}"
            raise FileNotFoundError(msg)

        return published_workflow_filename

    async def _execute_subprocess(
        self,
        published_workflow_filename: Path,
        file_name: str,
        pickle_control_flow_result: bool = True,  # noqa: FBT001, FBT002
        flow_input: dict[str, Any] | None = None,
        node: SubflowNodeGroup | None = None,
    ) -> dict[str, dict[str | SerializedNodeCommands.UniqueParameterValueUUID, Any] | None]:
        """Execute the published workflow in a subprocess.

        Args:
            published_workflow_filename: Path to the workflow file to execute
            file_name: Name of the workflow for logging
            pickle_control_flow_result: Whether to pickle control flow results (defaults to True)
            flow_input: Optional dictionary of parameter values to pass to the workflow's StartFlow node
            node: Optional SubflowNodeGroup to receive real-time event updates

        Returns:
            The subprocess execution output dictionary
        """
        from griptape_nodes.bootstrap.workflow_executors.subprocess_workflow_executor import (
            SubprocessWorkflowExecutor,
        )

        # Define event callback if node provided for GUI updates
        on_event: Callable[[dict], None] | None = None
        if node is not None:
            on_event = node.subflow_execution_component.handle_execution_event

        subprocess_executor = SubprocessWorkflowExecutor(
            workflow_path=str(published_workflow_filename),
            on_event=on_event,
        )
        try:
            async with subprocess_executor as executor:
                await executor.arun(
                    flow_input=flow_input or {},
                    storage_backend=await self._get_storage_backend(),
                    pickle_control_flow_result=pickle_control_flow_result,
                )
        except RuntimeError as e:
            # Subprocess returned non-zero exit code
            logger.error(
                "Subprocess execution failed for workflow '%s' at path '%s'. Error: %s",
                file_name,
                published_workflow_filename,
                e,
            )
            raise

        my_subprocess_result = subprocess_executor.output
        if my_subprocess_result is None:
            msg = f"Subprocess completed but returned no output for workflow '{file_name}'"
            logger.error(msg)
            raise ValueError(msg)
        return my_subprocess_result

    def _find_loop_entry_node(
        self, start_node: BaseIterativeStartNode, node_group_name: str | None, connections: Any
    ) -> EntryNodeParameter:
        """Find the entry control node and parameter for a loop body.

        Args:
            start_node: The loop start node
            node_group_name: Name of NodeGroup if loop body is a NodeGroup, None otherwise
            connections: Connections object from FlowManager

        Returns:
            Tuple of (entry_node_name, entry_parameter_name) or (None, None) if not found
        """
        entry_control_node_name = None
        entry_control_parameter_name = None
        exec_out_param_name = start_node.exec_out.name

        if start_node.name not in connections.outgoing_index:
            return EntryNodeParameter(None, None)

        exec_out_connections = connections.outgoing_index[start_node.name].get(exec_out_param_name, [])
        if not exec_out_connections:
            return EntryNodeParameter(None, None)

        first_conn_id = exec_out_connections[0]
        first_conn = connections.connections[first_conn_id]

        # If connecting to a NodeGroup, find the actual internal entry node
        if node_group_name is not None and first_conn.target_node.name == node_group_name:
            # The connection goes to a proxy parameter on the NodeGroup
            # Find the internal connection from that proxy parameter to the actual entry node
            proxy_param = first_conn.target_parameter
            if node_group_name in connections.outgoing_index:
                proxy_connections = connections.outgoing_index[node_group_name].get(proxy_param.name, [])
                if proxy_connections:
                    internal_conn_id = proxy_connections[0]
                    internal_conn = connections.connections[internal_conn_id]
                    if internal_conn.is_node_group_internal:
                        entry_control_node_name = internal_conn.target_node.name
                        entry_control_parameter_name = internal_conn.target_parameter.name
        else:
            # Direct connection to a regular node
            entry_control_node_name = first_conn.target_node.name
            entry_control_parameter_name = first_conn.target_parameter.name
            # If the connection is just to the End Node, then we don't have an entry control connection.
            if first_conn.target_node == start_node.end_node:
                return EntryNodeParameter(None, None)

        return EntryNodeParameter(entry_node=entry_control_node_name, entry_parameter=entry_control_parameter_name)

    def _collect_loop_body_nodes(
        self,
        start_node: BaseIterativeStartNode,
        end_node: BaseIterativeEndNode,
        nodes_in_control_flow: set[str],
        connections: Any,
    ) -> LoopBodyNodes:
        """Collect all nodes in the loop body, including data dependencies.

        Returns:
            LoopBodyNodes containing all_nodes, execution_type, and node_group_name
        """
        all_nodes: set[str] = set()
        visited_deps: set[str] = set()

        node_manager = GriptapeNodes.NodeManager()
        # Exclude the start node from packaging. And, we don't want their dependencies.
        nodes_in_control_flow.discard(start_node.name)
        for node_name in nodes_in_control_flow:
            # Add ALL nodes in control flow for removal from parent DAG
            all_nodes.add(node_name)
            node_obj = node_manager.get_node_by_name(node_name)
            deps = DagBuilder.collect_data_dependencies_for_node(
                node_obj, connections, nodes_in_control_flow, visited_deps
            )
            all_nodes.update(deps)
        # Discard the end node from packaging.
        all_nodes.discard(end_node.name)
        # Make sure the start node wasn't added in the dependencies.
        all_nodes.discard(start_node.name)

        # See if they're all in one NodeGroup
        execution_type = LOCAL_EXECUTION
        node_group_name = None
        if len(all_nodes) == 1:
            node_inside = all_nodes.pop()
            node_obj = node_manager.get_node_by_name(node_inside)
            if isinstance(node_obj, SubflowNodeGroup):
                execution_type = node_obj.get_parameter_value(node_obj.execution_environment.name)
                all_nodes.update(node_obj.get_all_nodes())
                node_group_name = node_obj.name
            else:
                all_nodes.add(node_inside)

        return LoopBodyNodes(all_nodes=all_nodes, execution_type=execution_type, node_group_name=node_group_name)

    async def _package_loop_body(
        self,
        start_node: BaseIterativeStartNode,
        end_node: BaseIterativeEndNode,
    ) -> tuple[PackageNodesAsSerializedFlowResultSuccess, str] | None:
        """Package the loop body (nodes between start and end) into a serialized flow.

        Args:
            start_node: The BaseIterativeStartNode marking the start of the loop
            end_node: The BaseIterativeEndNode marking the end of the loop
            execution_type: The execution environment type

        Returns:
            PackageNodesAsSerializedFlowResultSuccess if successful, None if empty loop body
        """
        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()

        # Collect all nodes in the forward control path from start to end
        nodes_in_control_flow = DagBuilder.collect_nodes_in_forward_control_path(start_node, end_node, connections)

        # Filter out nodes already in the current DAG and collect data dependencies
        loop_body_result = self._collect_loop_body_nodes(start_node, end_node, nodes_in_control_flow, connections)
        all_nodes = loop_body_result.all_nodes
        execution_type = loop_body_result.execution_type
        node_group_name = loop_body_result.node_group_name

        # Handle empty loop body (no nodes between start and end)
        if not all_nodes:
            await self._handle_empty_loop_body(start_node, end_node)
            return None
        # Find the first node in the loop body (where start_node.exec_out connects to)
        entry_node_parameter = self._find_loop_entry_node(start_node, node_group_name, connections)
        entry_control_node_name = entry_node_parameter.entry_node
        entry_control_parameter_name = entry_node_parameter.entry_parameter
        # Determine library and node types based on execution_type
        library = None
        if execution_type not in (LOCAL_EXECUTION, PRIVATE_EXECUTION):
            try:
                library = LibraryRegistry.get_library(name=execution_type)
            except KeyError:
                msg = "Could not find library '%s' for loop execution", execution_type
                logger.error(msg)
                raise RuntimeError(msg)  # noqa: B904

            library_name = library.get_library_data().name
        workflow_start_end_nodes = await self._get_workflow_start_end_nodes(library)
        start_node_type = workflow_start_end_nodes.start_flow_node_type
        end_node_type = workflow_start_end_nodes.end_flow_node_type
        library_name = workflow_start_end_nodes.start_flow_node_library_name

        # Create the packaging request
        request = PackageNodesAsSerializedFlowRequest(
            node_names=list(all_nodes),
            start_node_type=start_node_type,
            end_node_type=end_node_type,
            start_node_library_name=library_name,
            end_node_library_name=library_name,
            entry_control_node_name=entry_control_node_name,
            entry_control_parameter_name=entry_control_parameter_name,
            output_parameter_prefix=f"{end_node.name.replace(' ', '_')}_loop_",
            node_group_name=node_group_name,
        )

        package_result = GriptapeNodes.handle_request(request)
        if not isinstance(package_result, PackageNodesAsSerializedFlowResultSuccess):
            msg = f"Failed to package loop nodes for '{end_node.name}'. Error: {package_result.result_details}"
            raise TypeError(msg)

        logger.info(
            "Successfully packaged %d nodes for loop execution from '%s' to '%s'",
            len(all_nodes),
            start_node.name,
            end_node.name,
        )

        # Mark all packaged nodes as RESOLVED to prevent them from executing in the outer flow.
        # This is critical for nested loops: when an inner loop's body is packaged, those nodes
        # exist in the outer flow but should not execute there - they only execute in the packaged iterations.
        node_manager = GriptapeNodes.NodeManager()
        for node_name in all_nodes:
            node = node_manager.get_node_by_name(node_name)
            if node:
                node.state = NodeResolutionState.RESOLVED

        # Remove packaged nodes from global queue since they will be copied into loop iterations
        self._remove_packaged_nodes_from_queue(all_nodes)

        return package_result, execution_type

    async def _handle_empty_loop_body(
        self,
        start_node: BaseIterativeStartNode,
        end_node: BaseIterativeEndNode,
    ) -> None:
        """Handle empty loop body (no nodes between start and end).

        Args:
            start_node: The BaseIterativeStartNode
            end_node: The BaseIterativeEndNode
        """
        total_iterations = start_node._get_total_iterations()
        logger.info(
            "No nodes found between '%s' and '%s'. Processing empty loop body.",
            start_node.name,
            end_node.name,
        )

        # Check if there are direct data connections from start to end
        list_connections_request = ListConnectionsForNodeRequest(node_name=start_node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)

        connected_source_param = None
        if isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            for conn in list_connections_result.outgoing_connections:
                if conn.target_node_name == end_node.name and conn.target_parameter_name == "new_item_to_add":
                    connected_source_param = conn.source_parameter_name
                    break

        logger.info(
            "Processing %d iterations for empty loop from '%s' to '%s' (connected param: %s)",
            total_iterations,
            start_node.name,
            end_node.name,
            connected_source_param,
        )

        # Process iterations to collect results from direct connections
        end_node._results_list = []
        if connected_source_param:
            for iteration_index in range(total_iterations):
                start_node._current_iteration_count = iteration_index

                # Get the value based on which parameter is connected
                if connected_source_param == "current_item":
                    value = start_node._get_current_item_value()
                elif connected_source_param == "index":
                    value = start_node.get_current_index()
                else:
                    start_node._get_current_item_value()
                    value = start_node.parameter_output_values.get(connected_source_param)

                if value is not None:
                    end_node._results_list.append(value)

        end_node._output_results_list()

    def _should_break_loop(
        self,
        node_name_mappings: dict[str, str],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> bool:
        """Check if the loop should break based on the end node's control output.

        Args:
            node_name_mappings: Mapping from original to deserialized node names
            package_result: The package result containing parameter mappings

        Returns:
            True if the end node signaled a break, False otherwise
        """
        node_manager = GriptapeNodes.NodeManager()

        # Get the End node mapping
        end_node_mapping = self.get_node_parameter_mappings(package_result, "end")
        end_node_name = end_node_mapping.node_name

        # Get the deserialized end node name
        packaged_end_node_name = node_name_mappings.get(end_node_name)
        if packaged_end_node_name is None:
            logger.warning("Could not find deserialized End node name for %s", end_node_name)
            return False

        # Get the deserialized end node instance
        deserialized_end_node = node_manager.get_node_by_name(packaged_end_node_name)
        if deserialized_end_node is None:
            logger.warning("Could not find deserialized End node instance for %s", packaged_end_node_name)
            return False

        # Check if this is a BaseIterativeEndNode
        if not isinstance(deserialized_end_node, BaseIterativeEndNode):
            return False

        # Check if end node would emit break_loop_signal_output
        next_control_output = deserialized_end_node.get_next_control_output()
        if next_control_output is None:
            return False

        # Check if it's the break signal
        return next_control_output == deserialized_end_node.break_loop_signal_output

    def _get_iteration_control_action_for_group(
        self,
        end_loop_node: BaseIterativeNodeGroup,
        node_name_mappings: dict[str, str],
    ) -> IterationControlAction:
        """Determine which control action was taken during iteration for an iterative group.

        Checks if any internal nodes have executed their control outputs that connect to the
        group's control parameters (loop_complete, skip_iteration, break_loop). This is done by
        checking the source node's parameter_output_values for CONTROL_INPUT_PARAMETER.

        Args:
            end_loop_node: The BaseIterativeNodeGroup being executed
            node_name_mappings: Mapping from original to deserialized node names

        Returns:
            IterationControlAction indicating which control path was taken
        """
        # Get incoming connections to the end_loop_node (the iterative group)
        list_connections_request = ListConnectionsForNodeRequest(node_name=end_loop_node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            logger.warning("Failed to list connections for node %s", end_loop_node.name)
            return IterationControlAction.ADD

        incoming_connections = list_connections_result.incoming_connections

        # Check each control parameter to see if its source node has fired
        # Priority: BREAK > SKIP > LOOP_COMPLETE > default ADD
        break_source = self._find_source_for_control_param(incoming_connections, IterationControlParam.BREAK_LOOP)
        skip_source = self._find_source_for_control_param(incoming_connections, IterationControlParam.SKIP_ITERATION)
        loop_complete_source = self._find_source_for_control_param(
            incoming_connections, IterationControlParam.LOOP_COMPLETE
        )

        # Check if break was triggered
        if self._check_control_source_fired(break_source, node_name_mappings):
            return IterationControlAction.BREAK

        # Check if skip was triggered
        if self._check_control_source_fired(skip_source, node_name_mappings):
            return IterationControlAction.SKIP

        # Check if loop_complete was triggered
        if self._check_control_source_fired(loop_complete_source, node_name_mappings):
            return IterationControlAction.ADD

        # If none of the control parameters were triggered, default to ADD
        # This preserves backward compatibility for workflows without explicit loop_complete connections
        return IterationControlAction.ADD

    def _check_control_source_fired(
        self,
        source: tuple[str, str] | None,
        node_name_mappings: dict[str, str],
    ) -> bool:
        """Check if a control source node has fired its control output.

        Args:
            source: Tuple of (source_node_name, source_parameter_name) or None
            node_name_mappings: Mapping from original to deserialized node names

        Returns:
            True if the source node's next control output matches the specified parameter
        """
        if source is None:
            return False

        source_node_name, source_param_name = source
        deserialized_source_name = node_name_mappings.get(source_node_name)
        if deserialized_source_name is None:
            logger.debug("_check_control_source_fired: no deserialized name for '%s'", source_node_name)
            return False

        node_manager = GriptapeNodes.NodeManager()
        try:
            deserialized_source_node = node_manager.get_node_by_name(deserialized_source_name)
        except ValueError:
            logger.debug("_check_control_source_fired: node '%s' not found", deserialized_source_name)
            return False

        if deserialized_source_node is None:
            return False

        # Check if the node's next control output matches the source parameter
        next_control_output = deserialized_source_node.get_next_control_output()
        if next_control_output is None:
            logger.debug(
                "_check_control_source_fired: node '%s' next_control_output is None (state=%s, output_values=%s)",
                deserialized_source_name,
                deserialized_source_node.state,
                deserialized_source_node.parameter_output_values,
            )
            return False

        # Get the parameter object to compare
        source_param = deserialized_source_node.get_parameter_by_name(source_param_name)
        result = next_control_output == source_param
        logger.debug(
            "_check_control_source_fired: node '%s' next_control_output='%s' vs source_param='%s' -> %s",
            deserialized_source_name,
            next_control_output.name if next_control_output else None,
            source_param.name if source_param else None,
            result,
        )
        return result

    def _find_source_for_control_param(
        self,
        incoming_connections: list,
        control_param_name: str,
    ) -> tuple[str, str] | None:
        """Find the first source node and parameter that connects to a control parameter.

        Args:
            incoming_connections: List of incoming connections to the group
            control_param_name: Name of the control parameter to find (e.g., "break_loop")

        Returns:
            Tuple of (source_node_name, source_parameter_name), or None if not found
        """
        sources = self._find_sources_for_control_param(incoming_connections, control_param_name)
        return sources[0] if sources else None

    async def _execute_loop_iterations_sequentially(  # noqa: PLR0915, C901, PLR0912
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any], int, bool]:
        """Execute loop iterations sequentially by running one flow instance N times.

        Args:
            package_result: The packaged flow with parameter mappings
            total_iterations: Number of iterations to run
            parameter_values_per_iteration: Dict mapping iteration_index -> parameter values
            end_loop_node: The End Loop Node to extract results for

        Returns:
            Tuple of:
            - iteration_results: Dict mapping iteration_index -> result value
            - successful_iterations: List of iteration indices that executed without error
            - last_iteration_values: Dict mapping parameter names -> values from last iteration
            - skipped_count: Number of iterations that were skipped via skip control signal
            - break_occurred: True if the loop exited early due to a break signal
        """
        # Deserialize flow once
        context_manager = GriptapeNodes.ContextManager()
        event_manager = GriptapeNodes.EventManager()
        with EventSuppressionContext(event_manager, LOOP_EVENTS_TO_SUPPRESS):
            deserialize_request = DeserializeFlowFromCommandsRequest(
                serialized_flow_commands=package_result.serialized_flow_commands
            )
            deserialize_result = GriptapeNodes.handle_request(deserialize_request)
            if not isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess):
                msg = f"Failed to deserialize flow for sequential loop. Error: {deserialize_result.result_details}"
                raise TypeError(msg)

            flow_name = deserialize_result.flow_name
            node_name_mappings = deserialize_result.node_name_mappings

            # Pop the deserialized flow from context stack
            if context_manager.has_current_flow() and context_manager.get_current_flow().name == flow_name:
                context_manager.pop_flow()

        logger.info("Successfully deserialized flow for sequential execution: %s", flow_name)
        # Get node mappings
        start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
        start_node_name = start_node_mapping.node_name
        packaged_start_node_name = node_name_mappings.get(start_node_name)

        if packaged_start_node_name is None:
            msg = f"Could not find deserialized Start node (original: '{start_node_name}') for sequential loop"
            raise TypeError(msg)

        iteration_results: dict[int, Any] = {}
        successful_iterations: list[int] = []
        skipped_count = 0
        break_occurred = False

        # Build reverse mapping: packaged_name → original_name for event translation
        reverse_node_mapping = {
            packaged_name: original_name for original_name, packaged_name in node_name_mappings.items()
        }

        try:
            # Execute iterations one at a time
            for iteration_index in range(total_iterations):
                logger.info(
                    "Starting sequential iteration %d/%d for loop ending at '%s'",
                    iteration_index,
                    total_iterations,
                    end_loop_node.name,
                )
                # Set input values for this iteration
                parameter_values = parameter_values_per_iteration[iteration_index]

                for startflow_param_name, value_to_set in parameter_values.items():
                    set_value_request = SetParameterValueRequest(
                        node_name=packaged_start_node_name,
                        parameter_name=startflow_param_name,
                        value=value_to_set,
                    )
                    set_value_result = await GriptapeNodes.ahandle_request(set_value_request)
                    if not isinstance(set_value_result, SetParameterValueResultSuccess):
                        logger.warning(
                            "Failed to set parameter '%s' on Start node '%s' for iteration %d: %s",
                            startflow_param_name,
                            packaged_start_node_name,
                            iteration_index,
                            set_value_result.result_details,
                        )

                # Execute this iteration with event translation instead of suppression
                # This allows the UI to show the original nodes highlighting during loop execution
                logger.info(
                    "Executing subflow for iteration %d - flow: '%s', start_node: '%s'",
                    iteration_index,
                    flow_name,
                    packaged_start_node_name,
                )
                with EventTranslationContext(event_manager, reverse_node_mapping):
                    start_subflow_request = StartLocalSubflowRequest(
                        flow_name=flow_name,
                        start_node=packaged_start_node_name,
                        pickle_control_flow_result=False,
                    )
                    start_subflow_result = await GriptapeNodes.ahandle_request(start_subflow_request)

                if not isinstance(start_subflow_result, StartLocalSubflowResultSuccess):
                    msg = f"Sequential loop iteration {iteration_index} failed: {start_subflow_result.result_details}"
                    logger.warning(
                        "Sequential iteration %d failed for loop ending at '%s'. Will attempt to extract partial results. Error: %s",
                        iteration_index,
                        end_loop_node.name,
                        start_subflow_result.result_details,
                    )
                    # Don't immediately store None - try to extract results first in case there are partial results
                    # (e.g., nested loop that had some failures but still produced output)
                else:
                    successful_iterations.append(iteration_index)

                # For BaseIterativeNodeGroup, check control action to handle skip/break
                if isinstance(end_loop_node, BaseIterativeNodeGroup):
                    control_action = self._get_iteration_control_action_for_group(end_loop_node, node_name_mappings)

                    if control_action == IterationControlAction.SKIP:
                        logger.info(
                            "Skip detected at iteration %d/%d - skipping result collection",
                            iteration_index + 1,
                            total_iterations,
                        )
                        skipped_count += 1
                        continue

                    if control_action == IterationControlAction.BREAK:
                        logger.info(
                            "Break detected at iteration %d/%d - collecting result then stopping",
                            iteration_index + 1,
                            total_iterations,
                        )
                        # Extract result from this iteration before breaking
                        # (the work was done, so we should collect the result)
                        deserialized_flows = [(iteration_index, flow_name, node_name_mappings)]
                        single_iteration_results = self.get_parameter_values_from_iterations(
                            end_loop_node=end_loop_node,
                            deserialized_flows=deserialized_flows,
                            package_flow_result_success=package_result,
                        )
                        iteration_results.update(single_iteration_results)
                        break_occurred = True
                        break

                    # control_action == ADD: fall through to extract result

                # Extract result from this iteration
                deserialized_flows = [(iteration_index, flow_name, node_name_mappings)]
                single_iteration_results = self.get_parameter_values_from_iterations(
                    end_loop_node=end_loop_node,
                    deserialized_flows=deserialized_flows,
                    package_flow_result_success=package_result,
                )
                iteration_results.update(single_iteration_results)

                logger.info("Completed sequential iteration %d/%d", iteration_index + 1, total_iterations)

                # Update the original start node's progress bar and index display
                if isinstance(end_loop_node, BaseIterativeEndNode) and end_loop_node.start_node is not None:
                    original_start = end_loop_node.start_node
                    if hasattr(original_start, "_progress_bar"):
                        original_start._progress_bar.increment()
                    # Publish the per-iteration index so the UI shows the current value
                    all_values = original_start.get_all_iteration_values()
                    if iteration_index < len(all_values):
                        current_index = all_values[iteration_index]
                        original_start.parameter_output_values[IterativeNodeParam.INDEX.value] = current_index
                        original_start.publish_update_to_parameter(IterativeNodeParam.INDEX.value, current_index)
                    await asyncio.sleep(0)

                # Check if the end node signaled a break (for BaseIterativeEndNode)
                if self._should_break_loop(node_name_mappings, package_result):
                    logger.info(
                        "Loop break detected at iteration %d/%d - stopping execution early",
                        iteration_index + 1,
                        total_iterations,
                    )
                    break_occurred = True
                    break

            # Extract last iteration values from the last successful iteration
            last_successful_iteration = successful_iterations[-1] if successful_iterations else 0
            deserialized_flows = [(last_successful_iteration, flow_name, node_name_mappings)]
            last_iteration_values = self.get_last_iteration_values_for_packaged_nodes(
                deserialized_flows=deserialized_flows,
                package_result=package_result,
                total_iterations=len(successful_iterations),
            )

            return iteration_results, successful_iterations, last_iteration_values, skipped_count, break_occurred

        finally:
            # Cleanup - delete the flow
            with EventSuppressionContext(event_manager, {DeleteFlowResultSuccess, DeleteFlowResultFailure}):
                delete_request = DeleteFlowRequest(flow_name=flow_name)
                delete_result = await GriptapeNodes.ahandle_request(delete_request)
                if not isinstance(delete_result, DeleteFlowResultSuccess):
                    logger.warning(
                        "Failed to delete sequential loop flow '%s': %s",
                        flow_name,
                        delete_result.result_details,
                    )

    async def _handle_sequential_loop_execution(  # noqa: C901
        self, start_node: BaseIterativeStartNode, end_node: BaseIterativeEndNode
    ) -> None:
        """Handle sequential loop execution by running iterations one at a time.

        Args:
            start_node: The BaseIterativeStartNode marking the start of the loop
            end_node: The BaseIterativeEndNode marking the end of the loop
        """
        total_iterations = start_node._get_total_iterations()
        logger.info(
            "Executing loop sequentially from '%s' to '%s' for %d iterations",
            start_node.name,
            end_node.name,
            total_iterations,
        )

        # Initialize the progress bar so the UI shows 0% at loop start
        start_node._progress_bar.initialize(total_iterations)
        await asyncio.sleep(0)

        # Package the loop body (nodes between start and end)
        package_result_and_execution = await self._package_loop_body(start_node, end_node)

        # Handle empty loop body (no nodes between start and end)
        if package_result_and_execution is None:
            logger.info("Empty loop body - results already set by _package_loop_body")
            return
        package_result, execution_type = package_result_and_execution

        # Get parameter values per iteration
        parameter_values_per_iteration = self.get_parameter_values_per_iteration(start_node, package_result)

        # Get resolved upstream values (constant across all iterations)
        # Reuse the packaged_node_names from package_result instead of recalculating
        resolved_upstream_values = self.get_resolved_upstream_values(
            packaged_node_names=package_result.packaged_node_names, package_result=package_result
        )

        # Merge upstream values into each iteration (only if parameter doesn't already exist)
        if resolved_upstream_values:
            for iteration_index in parameter_values_per_iteration:
                for param_name, param_value in resolved_upstream_values.items():
                    if param_name not in parameter_values_per_iteration[iteration_index]:
                        parameter_values_per_iteration[iteration_index][param_name] = param_value

        # Execute iterations sequentially based on execution environment
        if execution_type == LOCAL_EXECUTION:
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
                _skipped_count,
                _break_occurred,
            ) = await self._execute_loop_iterations_sequentially(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_node,
            )
        elif execution_type == PRIVATE_EXECUTION:
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
            ) = await self._execute_loop_iterations_sequentially_private(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_node,
            )
        else:
            # Cloud publisher execution (Deadline Cloud, etc.)
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
            ) = await self._execute_loop_iterations_sequentially_via_publisher(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_node,
                execution_type=execution_type,
            )
        # Check if execution stopped early due to break (not failure)
        if len(successful_iterations) < total_iterations:
            # Only raise an error if there were actual failures (not just early termination)
            # If iterations stopped due to break, the last successful iteration count matches
            expected_count = len(successful_iterations)
            actual_count = len(iteration_results)
            if expected_count != actual_count:
                failed_count = expected_count - actual_count
                msg = f"Loop execution failed: {failed_count} of {expected_count} iterations failed"
                raise RuntimeError(msg)
            logger.info(
                "Loop execution stopped early at %d of %d iterations (break signal)",
                len(successful_iterations),
                total_iterations,
            )

        # Build results list in iteration order
        end_node._results_list = []
        for iteration_index in sorted(iteration_results.keys()):
            value = iteration_results[iteration_index]
            end_node._results_list.append(value)

        logger.info(
            "Loop '%s': Built results list with %d items from sequential iterations",
            end_node.name,
            len(end_node._results_list),
        )

        # Output final results to the results parameter
        end_node._output_results_list()
        logger.info("Loop '%s': Outputted final results list", end_node.name)

        # Apply last iteration values to the original packaged nodes
        self._apply_last_iteration_to_packaged_nodes(
            last_iteration_values=last_iteration_values,
            package_result=package_result,
        )
        logger.info("Loop '%s': Applied last iteration values to packaged nodes", end_node.name)

        logger.info(
            "Completed sequential loop execution from '%s' to '%s' with %d results",
            start_node.name,
            end_node.name,
            len(iteration_results),
        )

    def _get_merged_parameter_values_for_iterations(
        self, start_node: BaseIterativeStartNode, package_result: PackageNodesAsSerializedFlowResultSuccess
    ) -> dict[int, dict[str, Any]]:
        """Get parameter values for each iteration with resolved upstream values merged in.

        Args:
            start_node: The start node for the loop
            package_result: The packaged flow result containing parameter mappings

        Returns:
            Dict mapping iteration_index -> {parameter_name: value}
        """
        # Get parameter values from start node (vary per iteration)
        parameter_values_per_iteration = self.get_parameter_values_per_iteration(start_node, package_result)

        # Get resolved upstream values (constant across all iterations)
        resolved_upstream_values = self.get_resolved_upstream_values(
            packaged_node_names=package_result.packaged_node_names, package_result=package_result
        )

        # Merge upstream values into each iteration (only if parameter doesn't already exist)
        if resolved_upstream_values:
            for iteration_index in parameter_values_per_iteration:
                for param_name, param_value in resolved_upstream_values.items():
                    if param_name not in parameter_values_per_iteration[iteration_index]:
                        parameter_values_per_iteration[iteration_index][param_name] = param_value
            logger.info(
                "Added %d resolved upstream values to %d iterations",
                len(resolved_upstream_values),
                len(parameter_values_per_iteration),
            )

        return parameter_values_per_iteration

    async def handle_loop_execution(self, node: BaseIterativeEndNode) -> None:
        """Handle execution of a loop by packaging nodes from start to end and running them.

        Args:
            node: The BaseIterativeEndNode marking the end of the loop
            execution_type: The execution environment type
        """
        # Validate start node exists
        if node.start_node is None:
            msg = f"BaseIterativeEndNode '{node.name}' has no start_node reference"
            raise ValueError(msg)

        start_node = node.start_node

        # Initialize iteration data to determine total iterations
        start_node._initialize_iteration_data()

        total_iterations = start_node._get_total_iterations()
        if total_iterations == 0:
            logger.info("No iterations for empty loop from '%s' to '%s'", start_node.name, node.name)
            return

        # Check if we should run in order (default is in order / True)
        run_in_order = start_node.get_parameter_value("run_in_order")
        if run_in_order:
            # Sequential execution - run iterations one at a time in the main execution flow
            await self._handle_sequential_loop_execution(start_node, node)
            return

        # Parallel execution - package and run all iterations concurrently
        # Package the loop body (nodes between start and end)
        package_result_and_execution_type = await self._package_loop_body(start_node, node)

        # Handle empty loop body (no nodes between start and end)
        if package_result_and_execution_type is None:
            logger.info("Empty loop body - results already set by _package_loop_body")
            return
        package_result, execution_type = package_result_and_execution_type
        # Get parameter values for each iteration
        parameter_values_to_set_before_run = self._get_merged_parameter_values_for_iterations(
            start_node, package_result
        )

        # Step 5: Execute all iterations based on execution environment
        if execution_type == LOCAL_EXECUTION:
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
            ) = await self._execute_loop_iterations_locally(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_to_set_before_run,
                end_loop_node=node,
            )
        elif execution_type == PRIVATE_EXECUTION:
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
            ) = await self._execute_loop_iterations_privately(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_to_set_before_run,
                end_loop_node=node,
            )
        else:
            # Cloud publisher execution (Deadline Cloud, etc.)
            (
                iteration_results,
                successful_iterations,
                last_iteration_values,
            ) = await self._execute_loop_iterations_via_publisher(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_to_set_before_run,
                end_loop_node=node,
                execution_type=execution_type,
            )

        if len(successful_iterations) != total_iterations:
            failed_count = total_iterations - len(successful_iterations)
            logger.warning(
                "Loop execution: %d of %d iterations failed. Results will contain None for failed iterations.",
                failed_count,
                total_iterations,
            )

        logger.info(
            "Completed execution of %d iterations for loop '%s' (%d successful, %d failed)",
            total_iterations,
            start_node.name,
            len(successful_iterations),
            total_iterations - len(successful_iterations),
        )

        # Step 6: Build results list in iteration order, with None for failed iterations
        node._results_list = []
        for iteration_index in range(total_iterations):
            if iteration_index in iteration_results:
                value = iteration_results[iteration_index]
            else:
                value = None  # Failed iterations get None
            node._results_list.append(value)

        # Step 7: Output final results to the results parameter
        node._output_results_list()

        # Step 8: Apply last iteration values to the original packaged nodes in main flow
        self._apply_last_iteration_to_packaged_nodes(
            last_iteration_values=last_iteration_values,
            package_result=package_result,
        )

        logger.info(
            "Successfully aggregated %d results for loop '%s' to '%s'",
            len(iteration_results),
            start_node.name,
            node.name,
        )

    async def handle_while_group_execution(self, node: BaseWhileNodeGroup) -> None:
        """Handle execution of a while-loop node group by running its child nodes in a loop.

        This method:
        1. Packages the child nodes into a serialized flow
        2. Deserializes and executes the flow
        3. Checks which control input was triggered (done vs continue_loop)
        4. If continue_loop and iterations remain, re-executes the flow
        5. Propagates final results when done

        Args:
            node: The BaseWhileNodeGroup to execute
        """
        node._initialize_loop_data()
        max_iterations = node._get_max_iterations()

        # Get execution environment
        execution_type = node.get_parameter_value(node.execution_environment.name)

        if execution_type != LOCAL_EXECUTION:
            node.subflow_execution_component.clear_execution_state()

        # Package the group body (child nodes)
        package_result = await self._package_subflow_group_body(node, "while_group")

        if package_result is None:
            logger.debug("Empty while group '%s' - no child nodes to execute", node.name)
            node._on_complete(condition_met=True, iterations=0)
            return

        # Get resolved upstream values (constant across all iterations)
        resolved_upstream_values = self.get_resolved_upstream_values(
            packaged_node_names=package_result.packaged_node_names, package_result=package_result
        )

        # Find StartFlow parameter(s) that correspond to iteration
        start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
        iteration_startflow_params = self._get_while_iteration_param_mappings(
            node, start_node_mapping.parameter_mappings
        )

        # Deserialize the flow once for sequential re-execution
        flow_name, node_name_mappings, packaged_start_node_name = self._deserialize_while_flow(
            package_result, start_node_mapping.node_name
        )

        # Build reverse mapping for event translation
        reverse_node_mapping = {
            packaged_name: original_name for original_name, packaged_name in node_name_mappings.items()
        }

        condition_met = False
        last_iteration_values: dict[str, Any] = {}
        total_iterations = max_iterations + 1  # first iteration + re-iterations
        event_manager = GriptapeNodes.EventManager()

        try:
            condition_met = await self._run_while_loop_iterations(
                node=node,
                flow_name=flow_name,
                node_name_mappings=node_name_mappings,
                packaged_start_node_name=packaged_start_node_name,
                resolved_upstream_values=resolved_upstream_values,
                iteration_startflow_params=iteration_startflow_params,
                reverse_node_mapping=reverse_node_mapping,
                event_manager=event_manager,
                max_iterations=max_iterations,
                total_iterations=total_iterations,
            )

            # Extract final values from the deserialized flow
            deserialized_flows = [(0, flow_name, node_name_mappings)]
            last_iteration_values = self.get_last_iteration_values_for_packaged_nodes(
                deserialized_flows=deserialized_flows,
                package_result=package_result,
                total_iterations=1,
            )

        finally:
            # Clean up the deserialized flow
            with EventSuppressionContext(event_manager, LOOP_EVENTS_TO_SUPPRESS):
                delete_request = DeleteFlowRequest(flow_name=flow_name)
                delete_result = GriptapeNodes.handle_request(delete_request)
                if isinstance(delete_result, DeleteFlowResultFailure):
                    logger.warning("Failed to clean up while group flow '%s': %s", flow_name, delete_result)

        # Notify subclass and set base outputs
        total_iterations_executed = node._current_iteration + 1
        node._on_complete(condition_met=condition_met, iterations=total_iterations_executed)

        # Apply last iteration values to the original child nodes
        self._apply_last_iteration_to_packaged_nodes(
            last_iteration_values=last_iteration_values,
            package_result=package_result,
        )

        # Propagate output values from child nodes through proxy parameters
        node._propagate_output_values_from_internal_nodes()

        logger.debug(
            "While group '%s': completed after %d iteration(s), condition_met=%s",
            node.name,
            node._current_iteration + 1,
            condition_met,
        )

    def _deserialize_while_flow(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        start_node_name: str,
    ) -> tuple[str, dict[str, str], str]:
        """Deserialize a packaged while-loop flow and return execution context.

        Args:
            package_result: The packaged flow result
            start_node_name: Original name of the start node

        Returns:
            Tuple of (flow_name, node_name_mappings, packaged_start_node_name)
        """
        context_manager = GriptapeNodes.ContextManager()
        event_manager = GriptapeNodes.EventManager()
        with EventSuppressionContext(event_manager, LOOP_EVENTS_TO_SUPPRESS):
            deserialize_request = DeserializeFlowFromCommandsRequest(
                serialized_flow_commands=package_result.serialized_flow_commands
            )
            deserialize_result = GriptapeNodes.handle_request(deserialize_request)
            if not isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess):
                msg = f"Failed to deserialize flow for while group. Error: {deserialize_result.result_details}"
                raise TypeError(msg)

            flow_name = deserialize_result.flow_name
            node_name_mappings = deserialize_result.node_name_mappings

            if context_manager.has_current_flow() and context_manager.get_current_flow().name == flow_name:
                context_manager.pop_flow()

        packaged_start_node_name = node_name_mappings.get(start_node_name)
        if packaged_start_node_name is None:
            msg = f"Could not find deserialized Start node (original: '{start_node_name}') for while group"
            raise TypeError(msg)

        return flow_name, node_name_mappings, packaged_start_node_name

    async def _run_while_loop_iterations(  # noqa: PLR0913
        self,
        *,
        node: BaseWhileNodeGroup,
        flow_name: str,
        node_name_mappings: dict[str, str],
        packaged_start_node_name: str,
        resolved_upstream_values: dict[str, Any],
        iteration_startflow_params: list[str],
        reverse_node_mapping: dict[str, str],
        event_manager: Any,
        max_iterations: int,
        total_iterations: int,
    ) -> bool:
        """Execute while-loop iterations until the done condition is met or iterations are exhausted.

        Returns:
            True if the loop's done condition was met, False otherwise
        """
        for iteration in range(total_iterations):
            node._current_iteration = iteration
            logger.debug(
                "While group '%s': starting iteration %d/%d",
                node.name,
                iteration + 1,
                total_iterations,
            )

            if iteration > 0:
                node._before_loop_iteration(iteration, flow_name)

            await self._set_while_iteration_parameters(
                packaged_start_node_name=packaged_start_node_name,
                resolved_upstream_values=resolved_upstream_values,
                iteration_startflow_params=iteration_startflow_params,
                iteration=iteration,
            )

            # Execute the subflow
            with EventTranslationContext(event_manager, reverse_node_mapping):
                start_subflow_request = StartLocalSubflowRequest(
                    flow_name=flow_name,
                    start_node=packaged_start_node_name,
                    pickle_control_flow_result=False,
                )
                start_subflow_result = await GriptapeNodes.ahandle_request(start_subflow_request)

            execution_failed = isinstance(start_subflow_result, StartLocalSubflowResultFailure)

            if execution_failed:
                logger.warning(
                    "While group '%s' iteration %d execution error: %s",
                    node.name,
                    iteration + 1,
                    start_subflow_result.result_details,
                )

            result = self._evaluate_while_iteration_result(
                node=node,
                execution_failed=execution_failed,
                node_name_mappings=node_name_mappings,
                iteration=iteration,
                max_iterations=max_iterations,
            )

            if result is not None:
                return result

        return False

    async def _set_while_iteration_parameters(
        self,
        *,
        packaged_start_node_name: str,
        resolved_upstream_values: dict[str, Any],
        iteration_startflow_params: list[str],
        iteration: int,
    ) -> None:
        """Set parameter values on the StartFlow node for a while-loop iteration."""
        parameter_values: dict[str, Any] = {}
        if resolved_upstream_values:
            parameter_values.update(resolved_upstream_values)
        for startflow_param in iteration_startflow_params:
            parameter_values[startflow_param] = iteration

        for startflow_param_name, value_to_set in parameter_values.items():
            set_value_request = SetParameterValueRequest(
                node_name=packaged_start_node_name,
                parameter_name=startflow_param_name,
                value=value_to_set,
            )
            set_value_result = await GriptapeNodes.ahandle_request(set_value_request)
            if not isinstance(set_value_result, SetParameterValueResultSuccess):
                logger.warning(
                    "Failed to set parameter '%s' on Start node '%s' for iteration %d: %s",
                    startflow_param_name,
                    packaged_start_node_name,
                    iteration,
                    set_value_result.result_details,
                )

    def _evaluate_while_iteration_result(
        self,
        *,
        node: BaseWhileNodeGroup,
        execution_failed: bool,
        node_name_mappings: dict[str, str],
        iteration: int,
        max_iterations: int,
    ) -> bool | None:
        """Evaluate the result of a single while-loop iteration.

        Returns:
            True if done condition met, False if iterations exhausted, None if should continue looping
        """
        total_iterations = max_iterations + 1
        iterations_remaining = iteration < max_iterations

        # If the execution itself errored, treat as continue (retry) regardless of control signals
        if execution_failed:
            if iterations_remaining:
                logger.debug(
                    "While group '%s': execution error on iteration %d/%d, will continue",
                    node.name,
                    iteration + 1,
                    total_iterations,
                )
                return None
            logger.info(
                "While group '%s': execution error on iteration %d/%d, no iterations remaining",
                node.name,
                iteration + 1,
                total_iterations,
            )
            return False

        # Check which control input was triggered
        loop_action = self._get_while_control_action(node, node_name_mappings)

        if loop_action == WhileControlParam.DONE:
            logger.info("While group '%s': done on iteration %d/%d", node.name, iteration + 1, total_iterations)
            return True

        if loop_action == WhileControlParam.CONTINUE:
            if iterations_remaining:
                logger.debug(
                    "While group '%s': continuing on iteration %d/%d",
                    node.name,
                    iteration + 1,
                    total_iterations,
                )
                return None
            logger.info(
                "While group '%s': continue requested on iteration %d/%d, no iterations remaining",
                node.name,
                iteration + 1,
                total_iterations,
            )
            return False

        # Neither done nor continue was triggered and execution didn't error - treat as done
        logger.info(
            "While group '%s': completed without control signal on iteration %d, treating as done",
            node.name,
            iteration + 1,
        )
        return True

    def _resolve_outgoing_target_through_proxy(
        self,
        target_node_name: str,
        target_param_name: str,
    ) -> tuple[str, str]:
        """Resolve a connection target through SubflowNodeGroup proxy parameters.

        If the target is a SubflowNodeGroup, follows the internal outgoing connection
        to find the actual destination node and parameter.

        Args:
            target_node_name: Name of the direct target node
            target_param_name: Name of the direct target parameter

        Returns:
            Tuple of (resolved_node_name, resolved_param_name)
        """
        node_manager = GriptapeNodes.NodeManager()
        try:
            target_node = node_manager.get_node_by_name(target_node_name)
        except ValueError:
            return (target_node_name, target_param_name)

        if not isinstance(target_node, SubflowNodeGroup):
            return (target_node_name, target_param_name)

        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()
        proxy_param = target_node.get_parameter_by_name(target_param_name)
        if proxy_param:
            internal_connections = connections.get_all_outgoing_connections(target_node)
            for internal_conn in internal_connections:
                if internal_conn.source_parameter.name == target_param_name and internal_conn.is_node_group_internal:
                    return (internal_conn.target_node.name, internal_conn.target_parameter.name)

        return (target_node_name, target_param_name)

    def _get_while_iteration_param_mappings(
        self,
        node: BaseWhileNodeGroup,
        start_node_param_mappings: dict,
    ) -> list[str]:
        """Find StartFlow parameter names that correspond to the while group's iteration output.

        Traces outgoing connections from the while group's iteration parameter
        to internal nodes, then maps those targets to their StartFlow parameter names.

        Args:
            node: The while group node
            start_node_param_mappings: Mappings from StartFlow param names to original node/param

        Returns:
            List of StartFlow parameter names that should receive the iteration value
        """
        iteration_params: list[str] = []

        list_connections_request = ListConnectionsForNodeRequest(node_name=node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            logger.warning("Failed to list connections for while group node %s", node.name)
            return iteration_params

        for conn in list_connections_result.outgoing_connections:
            if conn.source_parameter_name != "iteration":
                continue

            target_node_name, target_param_name = self._resolve_outgoing_target_through_proxy(
                conn.target_node_name, conn.target_parameter_name
            )

            # Find the corresponding StartFlow parameter
            for startflow_param_name, original_node_param in start_node_param_mappings.items():
                if (
                    original_node_param.node_name == target_node_name
                    and original_node_param.parameter_name == target_param_name
                ):
                    iteration_params.append(startflow_param_name)
                    break

        return iteration_params

    def _get_while_control_action(
        self,
        while_node: BaseWhileNodeGroup,
        node_name_mappings: dict[str, str],
    ) -> WhileControlParam | None:
        """Determine which control action was taken during while group execution.

        Checks if internal nodes have triggered the 'done' or 'continue_loop' control
        inputs on the while group. Multiple nodes may connect to the same control input,
        so all sources are checked.

        Args:
            while_node: The BaseWhileNodeGroup being executed
            node_name_mappings: Mapping from original to deserialized node names

        Returns:
            WhileControlParam.DONE, WhileControlParam.CONTINUE, or None
        """
        list_connections_request = ListConnectionsForNodeRequest(node_name=while_node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            logger.warning("Failed to list connections for while group node %s", while_node.name)
            return None

        incoming_connections = list_connections_result.incoming_connections

        # Collect all sources for both control params
        done_sources = self._find_sources_for_control_param(incoming_connections, WhileControlParam.DONE)
        continue_sources = self._find_sources_for_control_param(incoming_connections, WhileControlParam.CONTINUE)

        # Check if any done source fired
        done_fired = any(self._check_control_source_fired(source, node_name_mappings) for source in done_sources)
        # Check if any continue source fired
        continue_fired = any(
            self._check_control_source_fired(source, node_name_mappings) for source in continue_sources
        )

        logger.debug(
            "While group '%s': done_fired=%s (sources=%s), continue_fired=%s (sources=%s)",
            while_node.name,
            done_fired,
            done_sources,
            continue_fired,
            continue_sources,
        )

        # If both fired (shouldn't normally happen), continue takes priority to be safe
        if continue_fired:
            return WhileControlParam.CONTINUE
        if done_fired:
            return WhileControlParam.DONE

        return None

    def _find_sources_for_control_param(
        self,
        incoming_connections: list,
        control_param_name: str,
    ) -> list[tuple[str, str]]:
        """Find all source nodes and parameters that connect to a control parameter.

        Returns all matching sources. Used by retry groups where multiple nodes
        may connect to the same control input, and by iterative groups (via
        _find_source_for_control_param) which only need the first match.

        Args:
            incoming_connections: List of incoming connections to the group
            control_param_name: Name of the control parameter to find

        Returns:
            List of (source_node_name, source_parameter_name) tuples
        """
        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()
        sources: list[tuple[str, str]] = []

        for conn in incoming_connections:
            if conn.target_parameter_name != control_param_name:
                continue

            source_node_name = conn.source_node_name
            source_param_name = conn.source_parameter_name

            # If source is a SubflowNodeGroup, follow the internal connection to get the actual source
            node_manager = GriptapeNodes.NodeManager()
            try:
                source_node = node_manager.get_node_by_name(source_node_name)
            except ValueError:
                continue

            if isinstance(source_node, SubflowNodeGroup):
                proxy_param = source_node.get_parameter_by_name(source_param_name)
                if proxy_param:
                    internal_connections = connections.get_all_incoming_connections(source_node)
                    for internal_conn in internal_connections:
                        if (
                            internal_conn.target_parameter.name == source_param_name
                            and internal_conn.is_node_group_internal
                        ):
                            source_node_name = internal_conn.source_node.name
                            source_param_name = internal_conn.source_parameter.name
                            break

            sources.append((source_node_name, source_param_name))

        return sources

    def _resolve_on_each_entry(self, node: SubflowNodeGroup) -> tuple[str | None, str | None]:
        """Return (entry_node_name, entry_param_name) from on_each connection, or (None, None)."""
        # on_each only exists on iterative groups; non-iterative subflows fall back to implicit child-discovery.
        if not isinstance(node, BaseIterativeNodeGroup):
            return None, None
        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()
        # Use node.on_each.name instead of a literal so renames stay in sync.
        # Control outputs are single-target (enforced in connections.py), so index 0 is the only connection.
        on_each_conns = connections.outgoing_index.get(node.name, {}).get(node.on_each.name, [])
        if not on_each_conns:
            return None, None
        first_conn = connections.connections[on_each_conns[0]]
        if first_conn.target_node.name == node.name:
            # on_each is a proxy port on the group boundary. When the user wires on_each → a child node,
            # two hops are stored: group→proxy (boundary edge) then proxy→child (internal edge).
            # Follow both to find the actual entry node inside the group.
            proxy_param = first_conn.target_parameter
            proxy_conns = connections.outgoing_index.get(node.name, {}).get(proxy_param.name, [])
            if proxy_conns:
                # Control outputs are single-target, so index 0 is the only connection.
                internal_conn = connections.connections[proxy_conns[0]]
                if internal_conn.is_node_group_internal:
                    return internal_conn.target_node.name, internal_conn.target_parameter.name
            return None, None
        return first_conn.target_node.name, first_conn.target_parameter.name

    async def _package_subflow_group_body(
        self, node: SubflowNodeGroup, label: str
    ) -> PackageNodesAsSerializedFlowResultSuccess | None:
        """Package the child nodes of a subflow group into a serialized flow.

        Args:
            node: The SubflowNodeGroup whose children should be packaged
            label: Label used for the output parameter prefix (e.g., "while_group", "iterative_group")

        Returns:
            PackageNodesAsSerializedFlowResultSuccess if successful, None if no child nodes
        """
        all_nodes = node.get_all_nodes()
        node_names = list(all_nodes.keys())

        if not node_names:
            return None

        execution_type = node.get_parameter_value(node.execution_environment.name)

        library = None
        if execution_type not in (LOCAL_EXECUTION, PRIVATE_EXECUTION):
            try:
                library = LibraryRegistry.get_library(name=execution_type)
            except KeyError:
                logger.error("Could not find library '%s' for %s execution", execution_type, label)
                raise

        workflow_start_end_nodes = await self._get_workflow_start_end_nodes(library)

        sanitized_node_name = node.name.replace(" ", "_")
        output_parameter_prefix = f"{sanitized_node_name}_{label}_"

        entry_control_node_name, entry_control_parameter_name = self._resolve_on_each_entry(node)

        request = PackageNodesAsSerializedFlowRequest(
            node_names=node_names,
            start_node_type=workflow_start_end_nodes.start_flow_node_type,
            end_node_type=workflow_start_end_nodes.end_flow_node_type,
            start_node_library_name=workflow_start_end_nodes.start_flow_node_library_name,
            end_node_library_name=workflow_start_end_nodes.end_flow_node_library_name,
            output_parameter_prefix=output_parameter_prefix,
            entry_control_node_name=entry_control_node_name,
            entry_control_parameter_name=entry_control_parameter_name,
            node_group_name=node.name,
        )

        package_result = GriptapeNodes.handle_request(request)
        if not isinstance(package_result, PackageNodesAsSerializedFlowResultSuccess):
            msg = f"Failed to package {label} '{node.name}'. Error: {package_result.result_details}"
            raise TypeError(msg)

        logger.info(
            "Successfully packaged %d nodes for %s '%s'",
            len(node_names),
            label,
            node.name,
        )

        # Mark packaged nodes as RESOLVED to prevent outer flow execution
        node_manager = GriptapeNodes.NodeManager()
        for node_name in node_names:
            node_reference = node_manager.get_node_by_name(node_name)
            if node_reference:
                node_reference.state = NodeResolutionState.RESOLVED

        self._remove_packaged_nodes_from_queue(set(node_names))

        return package_result

    async def handle_iterative_group_execution(self, node: BaseIterativeNodeGroup) -> None:
        """Handle execution of an iterative node group by running its child nodes for each iteration.

        This method is similar to handle_loop_execution but simplified for node groups:
        - Child nodes are already known (node.get_all_nodes())
        - No need to find/validate start-end node connections
        - The group itself holds iteration parameters (items, current_item, index, results)

        Args:
            node: The BaseIterativeNodeGroup to execute
        """
        # Initialize iteration data to determine total iterations
        node._initialize_iteration_data()

        total_iterations = node._get_total_iterations()
        if total_iterations == 0:
            logger.info("No iterations for empty iterative group '%s'", node.name)
            node._output_results_list()
            return

        # Get execution environment
        execution_type = node.get_parameter_value(node.execution_environment.name)

        # Clear execution state before subprocess execution starts (for non-local execution)
        if execution_type != LOCAL_EXECUTION:
            node.subflow_execution_component.clear_execution_state()

        # Check if we should run in order (default is sequential/True)
        run_in_order = node.get_parameter_value("run_in_order")

        if run_in_order:
            # Sequential execution
            await self._handle_sequential_iterative_group_execution(node, execution_type)
            return

        # Parallel execution - package and run all iterations concurrently
        package_result = await self._package_subflow_group_body(node, "iterative_group")

        # Handle empty group (no child nodes)
        if package_result is None:
            logger.info("Empty iterative group '%s' - no child nodes to execute", node.name)
            node._output_results_list()
            return

        # Get parameter values for each iteration
        parameter_values_to_set_before_run = self._get_merged_parameter_values_for_iterative_group(node, package_result)

        # Execute all iterations based on execution environment
        match execution_type:
            case node_types.LOCAL_EXECUTION:
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                ) = await self._execute_loop_iterations_locally(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_to_set_before_run,
                    end_loop_node=node,
                )
            case node_types.PRIVATE_EXECUTION:
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                ) = await self._execute_loop_iterations_privately(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_to_set_before_run,
                    end_loop_node=node,
                )
            case _:
                # Cloud publisher execution (Deadline Cloud, etc.)
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                ) = await self._execute_loop_iterations_via_publisher(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_to_set_before_run,
                    end_loop_node=node,
                    execution_type=execution_type,
                )

        if len(successful_iterations) != total_iterations:
            failed_count = total_iterations - len(successful_iterations)
            msg = f"Iterative group execution failed: {failed_count} of {total_iterations} iterations failed"
            raise RuntimeError(msg)

        logger.info(
            "Successfully completed parallel execution of %d iterations for iterative group '%s'",
            total_iterations,
            node.name,
        )

        # Build results list in iteration order
        node._results_list = []
        for iteration_index in sorted(iteration_results.keys()):
            value = iteration_results[iteration_index]
            node._results_list.append(value)

        # Output final results to the results parameter
        node._output_results_list()

        # Apply last iteration values to the original child nodes in main flow
        self._apply_last_iteration_to_packaged_nodes(
            last_iteration_values=last_iteration_values,
            package_result=package_result,
        )

        logger.info(
            "Successfully aggregated %d results for iterative group '%s'",
            len(iteration_results),
            node.name,
        )

    async def _handle_sequential_iterative_group_execution(
        self, node: BaseIterativeNodeGroup, execution_type: str
    ) -> None:
        """Handle sequential execution of an iterative node group.

        Args:
            node: The BaseIterativeNodeGroup to execute
            execution_type: The execution environment type
        """
        total_iterations = node._get_total_iterations()
        logger.info(
            "Executing iterative group '%s' sequentially for %d iterations",
            node.name,
            total_iterations,
        )

        # Package the group body (child nodes)
        package_result = await self._package_subflow_group_body(node, "iterative_group")

        # Handle empty group (no child nodes)
        if package_result is None:
            logger.info("Empty iterative group '%s' - no child nodes to execute", node.name)
            node._output_results_list()
            return

        # Get parameter values per iteration
        parameter_values_per_iteration = self._get_merged_parameter_values_for_iterative_group(node, package_result)

        # Execute iterations sequentially based on execution environment
        skipped_count = 0
        break_occurred = False
        match execution_type:
            case node_types.LOCAL_EXECUTION:
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                    skipped_count,
                    break_occurred,
                ) = await self._execute_loop_iterations_sequentially(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_per_iteration,
                    end_loop_node=node,
                )
            case node_types.PRIVATE_EXECUTION:
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                ) = await self._execute_loop_iterations_sequentially_private(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_per_iteration,
                    end_loop_node=node,
                )
            case _:
                # Cloud publisher execution
                (
                    iteration_results,
                    successful_iterations,
                    last_iteration_values,
                ) = await self._execute_loop_iterations_sequentially_via_publisher(
                    package_result=package_result,
                    total_iterations=total_iterations,
                    parameter_values_per_iteration=parameter_values_per_iteration,
                    end_loop_node=node,
                    execution_type=execution_type,
                )

        # Check if execution stopped early
        if len(successful_iterations) < total_iterations:
            if break_occurred:
                # Early exit due to break signal - this is expected behavior
                logger.info(
                    "Iterative group execution stopped early at %d of %d iterations (break signal)",
                    len(successful_iterations),
                    total_iterations,
                )
            else:
                # Early exit not due to break - check for failures
                # successful_iterations includes skipped iterations, but iteration_results does not
                # So we need to account for skipped iterations when checking for failures
                expected_result_count = len(successful_iterations) - skipped_count
                actual_result_count = len(iteration_results)
                if expected_result_count != actual_result_count:
                    failed_count = expected_result_count - actual_result_count
                    msg = f"Iterative group execution failed: {failed_count} of {len(successful_iterations)} iterations failed"
                    raise RuntimeError(msg)

        # Build results list in iteration order
        node._results_list = []
        for iteration_index in sorted(iteration_results.keys()):
            value = iteration_results[iteration_index]
            node._results_list.append(value)

        logger.info(
            "Iterative group '%s': Built results list with %d items from sequential iterations",
            node.name,
            len(node._results_list),
        )

        # Output final results
        node._output_results_list()

        # Apply last iteration values to the original child nodes
        self._apply_last_iteration_to_packaged_nodes(
            last_iteration_values=last_iteration_values,
            package_result=package_result,
        )

        logger.info(
            "Completed sequential iterative group execution for '%s' with %d results",
            node.name,
            len(iteration_results),
        )

    def _get_merged_parameter_values_for_iterative_group(
        self, node: BaseIterativeNodeGroup, package_result: PackageNodesAsSerializedFlowResultSuccess
    ) -> dict[int, dict[str, Any]]:
        """Get parameter values for each iteration with resolved upstream values merged in.

        Args:
            node: The iterative node group
            package_result: The packaged flow result

        Returns:
            Dict mapping iteration_index -> {parameter_name: value}
        """
        # Get parameter values that vary per iteration (current_item, index mappings)
        parameter_values_per_iteration = self.get_parameter_values_per_iteration(node, package_result)

        # Get resolved upstream values (constant across all iterations)
        resolved_upstream_values = self.get_resolved_upstream_values(
            packaged_node_names=package_result.packaged_node_names, package_result=package_result
        )

        # Merge upstream values into each iteration
        if resolved_upstream_values:
            for iteration_index in parameter_values_per_iteration:
                for param_name, param_value in resolved_upstream_values.items():
                    if param_name not in parameter_values_per_iteration[iteration_index]:
                        parameter_values_per_iteration[iteration_index][param_name] = param_value
            logger.info(
                "Added %d resolved upstream values to %d iterations for group '%s'",
                len(resolved_upstream_values),
                len(parameter_values_per_iteration),
                node.name,
            )

        return parameter_values_per_iteration

    def _get_iteration_value_for_parameter(
        self,
        source_param_name: str,
        iteration_index: int,
        index_values: list[int],
        current_item_values: list[Any],
    ) -> Any:
        """Get the value for a specific parameter at a given iteration.

        Args:
            source_param_name: Name of the source parameter (e.g., "index" or "current_item")
            iteration_index: 0-based iteration index
            index_values: List of actual loop values for ForLoop nodes
            current_item_values: List of items for ForEach nodes

        Returns:
            The value to set for this parameter at this iteration
        """
        if source_param_name == "index":
            # For ForLoop nodes, use actual loop value; otherwise use iteration_index
            if index_values and iteration_index < len(index_values):
                return index_values[iteration_index]
            return iteration_index
        if source_param_name == "current_item" and iteration_index < len(current_item_values):
            return current_item_values[iteration_index]
        return None

    def get_parameter_values_per_iteration(
        self,
        iteration_source: BaseIterativeStartNode | BaseIterativeNodeGroup,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> dict[int, dict[str, Any]]:
        """Get parameter values for each iteration of the loop.

        This maps iteration index to parameter values that should be set on the packaged flow's StartFlow node.
        Useful for: setting local values, sending as input for cloud publishing, or private workflow execution.

        Args:
            iteration_source: The node providing iteration values (BaseIterativeStartNode or BaseIterativeNodeGroup)
            package_result: PackageNodesAsSerializedFlowResultSuccess containing parameter_name_mappings

        Returns:
            Dict mapping iteration_index -> {startflow_param_name: value}
        """
        total_iterations = iteration_source._get_total_iterations()

        # Calculate current_item values for ForEach nodes
        iteration_items = iteration_source._get_iteration_items()
        current_item_values = list(iteration_items)

        # Calculate index values for ForLoop nodes
        # For ForLoop, we need actual loop values (start, start+step, start+2*step, ...)
        # not just 0-based iteration indices
        index_values = iteration_source.get_all_iteration_values()

        list_connections_request = ListConnectionsForNodeRequest(node_name=iteration_source.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            msg = (
                f"Failed to list connections for node {iteration_source.name}: {list_connections_result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 This should be a runtime error because it happens during execution.
        # Build parameter values for each iteration
        outgoing_connections = list_connections_result.outgoing_connections

        # Get Start node's parameter mappings (index 0 in the list)
        start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
        start_node_param_mappings = start_node_mapping.parameter_mappings

        # For each outgoing connection from iteration_source, find the corresponding StartFlow parameter
        # The start_node_param_mappings tells us: startflow_param_name -> OriginalNodeParameter(target_node, target_param)
        # We need to match the target of each connection to find the right startflow parameter
        parameter_val_mappings = {}
        for iteration_index in range(total_iterations):
            iteration_values = {}
            # iteration_values is going to be startflow parameter name -> value to set

            # For each outgoing data connection from iteration_source
            for conn in outgoing_connections:
                source_param_name = conn.source_parameter_name
                target_node_name, target_param_name = self._resolve_outgoing_target_through_proxy(
                    conn.target_node_name, conn.target_parameter_name
                )

                # Find the target parameter that corresponds to this target
                for startflow_param_name, original_node_param in start_node_param_mappings.items():
                    if (
                        original_node_param.node_name == target_node_name
                        and original_node_param.parameter_name == target_param_name
                    ):
                        # This StartFlow parameter feeds the target - set the appropriate value
                        value = self._get_iteration_value_for_parameter(
                            source_param_name, iteration_index, index_values, current_item_values
                        )
                        if value is not None:
                            iteration_values[startflow_param_name] = value
                        break

            parameter_val_mappings[iteration_index] = iteration_values

        return parameter_val_mappings

    def get_resolved_upstream_values(
        self,
        packaged_node_names: list[str],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> dict[str, Any]:
        """Collect parameter values from resolved upstream nodes outside the loop.

        When nodes inside the loop have connections to nodes outside that have already
        executed (RESOLVED state), we need to pass those values into the packaged flow
        via the StartFlow node parameters.

        Args:
            packaged_node_names: List of node names being packaged in the loop
            package_result: PackageNodesAsSerializedFlowResultSuccess containing parameter_name_mappings

        Returns:
            Dict mapping startflow_param_name -> value from resolved upstream node
        """
        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()
        node_manager = GriptapeNodes.NodeManager()

        # Get Start node's parameter mappings (index 0 in the list)
        start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
        start_node_param_mappings = start_node_mapping.parameter_mappings

        resolved_upstream_values = {}

        # For each packaged node, check its incoming data connections
        for packaged_node_name in packaged_node_names:
            try:
                packaged_node = node_manager.get_node_by_name(packaged_node_name)
            except Exception:
                logger.warning("Could not find packaged node '%s' to check upstream connections", packaged_node_name)
                continue

            # Check each parameter for incoming connections
            for param in packaged_node.parameters:
                # Skip control parameters
                if param.type == ParameterTypeBuiltin.CONTROL_TYPE:
                    continue

                # Get upstream connection
                upstream_connection = connections.get_connected_node(packaged_node, param)
                if not upstream_connection:
                    continue

                upstream_node, upstream_param = upstream_connection

                # Get upstream value if it meets criteria (resolved, not internal)
                upstream_value = self._get_upstream_connection_value(upstream_node, upstream_param, packaged_node_names)
                if upstream_value is None:
                    continue

                # Find the corresponding StartFlow parameter name
                startflow_param_name = self._map_to_startflow_parameter(
                    packaged_node_name, param.name, start_node_param_mappings
                )
                if startflow_param_name:
                    resolved_upstream_values[startflow_param_name] = upstream_value
                    logger.debug(
                        "Collected resolved upstream value: %s.%s -> StartFlow.%s = %s",
                        upstream_node.name,
                        upstream_param.name,
                        startflow_param_name,
                        upstream_value,
                    )

        logger.info("Collected %d resolved upstream values for loop execution", len(resolved_upstream_values))
        return resolved_upstream_values

    def _get_upstream_connection_value(
        self,
        upstream_node: BaseNode,
        upstream_param: Any,
        packaged_node_names: list[str],
    ) -> Any | None:
        """Extract value from upstream node if it meets criteria.

        Args:
            upstream_node: The upstream node that provides the value
            upstream_param: The parameter on the upstream node
            packaged_node_names: List of packaged node names to exclude internal connections

        Returns:
            The upstream value if criteria met, None otherwise
        """
        # If upstream is a SubflowNodeGroup (e.g., ForEach Group, Retry Group) that's currently executing,
        # we need to trace through its proxy parameter to find the actual resolved source.
        # This handles the case where: ExternalNode -> Group.proxy_param -> InternalNode
        if isinstance(upstream_node, SubflowNodeGroup) and upstream_node.state != NodeResolutionState.RESOLVED:
            return self._get_value_through_subflow_group_proxy(upstream_node, upstream_param, packaged_node_names)

        if upstream_node.state != NodeResolutionState.RESOLVED:
            return None

        if upstream_node.name in packaged_node_names:
            return None

        if upstream_param.name in upstream_node.parameter_output_values:
            return upstream_node.parameter_output_values[upstream_param.name]

        return upstream_node.get_parameter_value(upstream_param.name)

    def _get_value_through_subflow_group_proxy(
        self,
        subflow_group: SubflowNodeGroup,
        proxy_param: Any,
        packaged_node_names: list[str],
    ) -> Any | None:
        """Trace through a subflow group's proxy parameter to get value from the actual resolved source.

        When a packaged node inside a group has an incoming connection from the group's
        proxy parameter, we need to find the external node that connects TO that proxy parameter
        and get the value from there.

        Connection chain: ResolvedExternalNode -> Group.proxy_param -> PackagedInternalNode
        We want to get the value from ResolvedExternalNode.

        Args:
            subflow_group: The SubflowNodeGroup with the proxy parameter
            proxy_param: The proxy parameter on the group
            packaged_node_names: List of packaged node names to exclude

        Returns:
            The value from the resolved external source, or None if not found
        """
        flow_manager = GriptapeNodes.FlowManager()
        connections = flow_manager.get_connections()

        # Find the incoming connection TO the proxy parameter on the group
        # This will give us the actual external source node
        incoming_to_proxy = connections.get_incoming_connections_to_parameter(subflow_group, proxy_param)

        for conn in incoming_to_proxy:
            # Skip internal connections (from nodes inside the group)
            if conn.is_node_group_internal:
                continue

            source_node = conn.source_node
            source_param = conn.source_parameter

            # Skip if the source is also inside the packaged nodes
            if source_node.name in packaged_node_names:
                continue

            # The source must be resolved for us to get its value
            if source_node.state != NodeResolutionState.RESOLVED:
                logger.debug(
                    "Source node '%s' for proxy param '%s.%s' is not resolved (state: %s)",
                    source_node.name,
                    subflow_group.name,
                    proxy_param.name,
                    source_node.state,
                )
                continue

            # Get the value from the resolved source node
            if source_param.name in source_node.parameter_output_values:
                value = source_node.parameter_output_values[source_param.name]
            else:
                value = source_node.get_parameter_value(source_param.name)

            logger.debug(
                "Traced through proxy: %s.%s -> %s.%s (value type: %s)",
                source_node.name,
                source_param.name,
                subflow_group.name,
                proxy_param.name,
                type(value).__name__ if value is not None else "None",
            )
            return value

        return None

    def _map_to_startflow_parameter(
        self,
        packaged_node_name: str,
        param_name: str,
        start_node_param_mappings: dict[str, Any],
    ) -> str | None:
        """Find the StartFlow parameter name that maps to a packaged node parameter.

        Args:
            packaged_node_name: Name of the packaged node
            param_name: Name of the parameter on the packaged node
            start_node_param_mappings: Dict mapping startflow_param_name -> OriginalNodeParameter

        Returns:
            The StartFlow parameter name if found, None otherwise
        """
        for startflow_param_name, original_node_param in start_node_param_mappings.items():
            if original_node_param.node_name == packaged_node_name and original_node_param.parameter_name == param_name:
                return startflow_param_name
        return None

    def _find_endflow_param_for_end_loop_node(
        self,
        incoming_connections: list,
        end_node_param_mappings: dict,
    ) -> str | None:
        """Find the EndFlow parameter name that corresponds to BaseIterativeEndNode's new_item_to_add.

        Args:
            incoming_connections: List of incoming connections to end_loop_node
            end_node_param_mappings: Parameter mappings from EndFlow node

        Returns:
            Sanitized parameter name on EndFlow node, or None if not found
        """
        for conn in incoming_connections:
            if conn.target_parameter_name == "new_item_to_add":
                source_node_name = conn.source_node_name
                source_param_name = conn.source_parameter_name

                # If source is a NodeGroup, follow the internal connection to get the actual source
                node_manager = GriptapeNodes.NodeManager()
                flow_manager = GriptapeNodes.FlowManager()
                try:
                    source_node = node_manager.get_node_by_name(source_node_name)
                except ValueError:
                    continue
                if isinstance(source_node, SubflowNodeGroup):
                    # Get connections to this proxy parameter to find the actual internal source
                    connections = flow_manager.get_connections()
                    proxy_param = source_node.get_parameter_by_name(source_param_name)
                    if proxy_param:
                        internal_connections = connections.get_all_incoming_connections(source_node)
                        for internal_conn in internal_connections:
                            if (
                                internal_conn.target_parameter.name == source_param_name
                                and internal_conn.is_node_group_internal
                            ):
                                source_node_name = internal_conn.source_node.name
                                source_param_name = internal_conn.source_parameter.name
                                break

                # Find the EndFlow parameter that corresponds to this source
                for sanitized_param_name, original_node_param in end_node_param_mappings.items():
                    if (
                        original_node_param.node_name == source_node_name
                        and original_node_param.parameter_name == source_param_name
                    ):
                        return sanitized_param_name

        return None

    def get_node_parameter_mappings(
        self, package_result: PackageNodesAsSerializedFlowResultSuccess, start_or_end: str
    ) -> PackagedNodeParameterMapping:
        if start_or_end.lower() == "start":
            return package_result.parameter_name_mappings[0]
        if start_or_end.lower() == "end":
            return package_result.parameter_name_mappings[1]
        msg = f"start_or_end must be 'start' or 'end', got {start_or_end}"
        raise ValueError(msg)

    def get_parameter_values_from_iterations(
        self,
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
        deserialized_flows: list[tuple[int, str, dict[str, str]]],
        package_flow_result_success: PackageNodesAsSerializedFlowResultSuccess,
    ) -> dict[int, Any]:
        """Extract parameter values from each iteration's EndFlow node.

        The BaseIterativeEndNode is NOT packaged. Instead, we find what connects TO it,
        then extract those values from the packaged EndFlow node.

        Mirrors get_parameter_values_per_iteration pattern but works in reverse.

        Args:
            end_loop_node: The End Loop Node (NOT packaged, just used for reference)
            deserialized_flows: List of (iteration_index, flow_name, node_name_mappings)
            package_flow_result_success: PackageNodesAsSerializedFlowResultSuccess containing parameter_name_mappings

        Returns:
            Dict mapping iteration_index -> value for that iteration
        """
        # Step 1: Get incoming connections TO the end_loop_node
        list_connections_request = ListConnectionsForNodeRequest(node_name=end_loop_node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            msg = f"Failed to list connections for node {end_loop_node.name}: {list_connections_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004

        incoming_connections = list_connections_result.incoming_connections

        # Step 2: Get End node's parameter mappings (index 1 = EndFlow node)

        end_node_mapping = self.get_node_parameter_mappings(package_flow_result_success, "end")
        end_node_param_mappings = end_node_mapping.parameter_mappings

        # Step 3: Find the EndFlow parameter that corresponds to new_item_to_add
        endflow_param_name = self._find_endflow_param_for_end_loop_node(incoming_connections, end_node_param_mappings)

        if endflow_param_name is None:
            logger.warning(
                "No connections found to BaseIterativeEndNode '%s' new_item_to_add parameter. No results will be collected.",
                end_loop_node.name,
            )
            return {}

        # Step 4: Extract values from each iteration's EndFlow node
        packaged_end_node_name = end_node_mapping.node_name
        iteration_results = {}
        node_manager = GriptapeNodes.NodeManager()

        for iteration_index, flow_name, node_name_mappings in deserialized_flows:
            deserialized_end_node_name = node_name_mappings.get(packaged_end_node_name)
            if deserialized_end_node_name is None:
                logger.warning(
                    "Could not find deserialized End node for iteration %d in flow '%s'",
                    iteration_index,
                    flow_name,
                )
                continue

            try:
                deserialized_end_node = node_manager.get_node_by_name(deserialized_end_node_name)
                if endflow_param_name in deserialized_end_node.parameter_output_values:
                    extracted_value = deserialized_end_node.parameter_output_values[endflow_param_name]
                    iteration_results[iteration_index] = extracted_value
            except Exception as e:
                logger.warning(
                    "Failed to extract result from End node for iteration %d: %s",
                    iteration_index,
                    e,
                )
        return iteration_results

    def get_last_iteration_values_for_packaged_nodes(
        self,
        deserialized_flows: list[tuple[int, str, dict[str, str]]],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
    ) -> dict[str, Any]:
        """Extract parameter values from the LAST iteration's End Flow node for all output parameters.

        Returns values in same format as _extract_parameter_output_values(), ready to pass to
        _apply_parameter_values_to_node(). This sets the final state of packaged nodes after loop completes.

        Args:
            deserialized_flows: List of (iteration_index, flow_name, node_name_mappings)
            package_result: PackageNodesAsSerializedFlowResultSuccess containing parameter mappings
            total_iterations: Total number of iterations that were executed

        Returns:
            Dict mapping sanitized parameter names -> values from last iteration's End node
        """
        if total_iterations == 0:
            return {}

        last_iteration_index = total_iterations - 1

        # Find the last iteration in deserialized_flows
        last_iteration_flow = None
        for iteration_index, flow_name, node_name_mappings in deserialized_flows:
            if iteration_index == last_iteration_index:
                last_iteration_flow = (iteration_index, flow_name, node_name_mappings)
                break

        if last_iteration_flow is None:
            logger.warning(
                "Could not find last iteration (index %d) in deserialized flows. Cannot extract final values.",
                last_iteration_index,
            )
            return {}

        # Get End node's parameter mappings (index 1 = EndFlow node)
        end_node_mapping = self.get_node_parameter_mappings(package_result, "end")
        packaged_end_node_name = end_node_mapping.node_name

        # Get the deserialized End node name for last iteration
        _, _, node_name_mappings = last_iteration_flow
        deserialized_end_node_name = node_name_mappings.get(packaged_end_node_name)

        if deserialized_end_node_name is None:
            logger.warning(
                "Could not find deserialized End node (packaged name: '%s') in last iteration",
                packaged_end_node_name,
            )
            return {}

        # Get the End node instance
        node_manager = GriptapeNodes.NodeManager()
        try:
            deserialized_end_node = node_manager.get_node_by_name(deserialized_end_node_name)
        except Exception as e:
            logger.warning("Failed to get End node '%s' for last iteration: %s", deserialized_end_node_name, e)
            return {}

        # Extract ALL parameter output values from the End node
        # Return them with sanitized names (as they appear on End node)
        last_iteration_values = {}
        for sanitized_param_name in end_node_mapping.parameter_mappings:
            if sanitized_param_name in deserialized_end_node.parameter_output_values:
                last_iteration_values[sanitized_param_name] = deserialized_end_node.parameter_output_values[
                    sanitized_param_name
                ]

        logger.debug(
            "Extracted %d parameter values from last iteration's End node '%s'",
            len(last_iteration_values),
            deserialized_end_node_name,
        )

        return last_iteration_values

    async def _execute_loop_iterations_locally(  # noqa: C901, PLR0912, PLR0915
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations locally by deserializing and running flows.

        This method handles LOCAL execution of loop iterations. Other libraries
        can implement their own execution strategies (cloud, remote, etc.) by
        creating similar methods with the same signature.

        Args:
            package_result: The packaged flow with parameter mappings
            total_iterations: Number of iterations to run
            parameter_values_per_iteration: Dict mapping iteration_index -> parameter values
            end_loop_node: The End Loop Node to extract results for

        Returns:
            Tuple of:
            - iteration_results: Dict mapping iteration_index -> result value
            - successful_iterations: List of iteration indices that succeeded
            - last_iteration_values: Dict mapping parameter names -> values from last iteration
        """
        # Step 1: Deserialize N flow instances from the serialized flow
        # Save the current context and restore it after each deserialization to prevent
        # iteration flows from becoming children of each other
        deserialized_flows = []
        context_manager = GriptapeNodes.ContextManager()
        saved_context_flow = context_manager.get_current_flow() if context_manager.has_current_flow() else None

        # Suppress events during deserialization to prevent sending them to websockets
        event_manager = GriptapeNodes.EventManager()
        with EventSuppressionContext(event_manager, LOOP_EVENTS_TO_SUPPRESS):
            for iteration_index in range(total_iterations):
                # Restore context before each deserialization to ensure all iteration flows
                # are created at the same level (not as children of each other)
                if saved_context_flow is not None:
                    # Pop any flows that were pushed during previous iteration
                    while (
                        context_manager.has_current_flow() and context_manager.get_current_flow() != saved_context_flow
                    ):
                        context_manager.pop_flow()

                deserialize_request = DeserializeFlowFromCommandsRequest(
                    serialized_flow_commands=package_result.serialized_flow_commands
                )
                deserialize_result = GriptapeNodes.handle_request(deserialize_request)
                if not isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess):
                    msg = f"Failed to deserialize flow for iteration {iteration_index}. Error: {deserialize_result.result_details}"
                    raise TypeError(msg)

                deserialized_flows.append(
                    (iteration_index, deserialize_result.flow_name, deserialize_result.node_name_mappings)
                )

                # Pop the deserialized flow from the context stack to prevent it from staying there
                # Deserialization pushes the flow onto the stack, but we don't want iteration flows
                # to remain on the stack after deserialization
                if (
                    context_manager.has_current_flow()
                    and context_manager.get_current_flow().name == deserialize_result.flow_name
                ):
                    context_manager.pop_flow()
        logger.info("Successfully deserialized %d flow instances for parallel execution", total_iterations)
        # Step 2: Set input values on start nodes for each iteration
        for iteration_index, _, node_name_mappings in deserialized_flows:
            parameter_values = parameter_values_per_iteration[iteration_index]

            # Get Start node mapping (index 0 in the list)
            start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
            start_node_name = start_node_mapping.node_name
            start_params = start_node_mapping.parameter_mappings

            # Find the deserialized name for the Start node
            deserialized_start_node_name = node_name_mappings.get(start_node_name)
            if deserialized_start_node_name is None:
                logger.warning(
                    "Could not find deserialized Start node (original: '%s') for iteration %d",
                    start_node_name,
                    iteration_index,
                )
                continue

            # Set all parameter values on the deserialized Start node
            for startflow_param_name in start_params:
                if startflow_param_name not in parameter_values:
                    continue

                value_to_set = parameter_values[startflow_param_name]

                set_value_request = SetParameterValueRequest(
                    node_name=deserialized_start_node_name,
                    parameter_name=startflow_param_name,
                    value=value_to_set,
                )
                set_value_result = await GriptapeNodes.ahandle_request(set_value_request)
                if not isinstance(set_value_result, SetParameterValueResultSuccess):
                    logger.warning(
                        "Failed to set parameter '%s' on Start node '%s' for iteration %d: %s",
                        startflow_param_name,
                        deserialized_start_node_name,
                        iteration_index,
                        set_value_result.result_details,
                    )

        logger.info("Successfully set input values for %d iterations", total_iterations)

        # Step 3: Run all flows concurrently
        packaged_start_node_name = self.get_node_parameter_mappings(package_result, "start").node_name

        async def run_single_iteration(flow_name: str, iteration_index: int, start_node_name: str) -> tuple[int, bool]:
            """Run a single iteration flow and return success status."""
            # Suppress execution events during parallel iteration to prevent flooding websockets
            with EventSuppressionContext(event_manager, EXECUTION_EVENTS_TO_SUPPRESS):
                start_subflow_request = StartLocalSubflowRequest(
                    flow_name=flow_name,
                    start_node=start_node_name,
                    pickle_control_flow_result=False,
                )
                start_subflow_result = await GriptapeNodes.ahandle_request(start_subflow_request)
                success = isinstance(start_subflow_result, StartLocalSubflowResultSuccess)
                return iteration_index, success

        try:
            # Run all iterations concurrently
            iteration_tasks = [
                run_single_iteration(
                    flow_name,
                    iteration_index,
                    node_name_mappings.get(packaged_start_node_name),
                )
                for iteration_index, flow_name, node_name_mappings in deserialized_flows
            ]
            iteration_results = await asyncio.gather(*iteration_tasks, return_exceptions=True)

            # Step 4: Collect successful and failed iterations
            successful_iterations = []
            failed_iteration_indices = []
            failed_iteration_errors = {}  # Map iteration_index -> error

            for idx, result in enumerate(iteration_results):
                if isinstance(result, Exception):
                    # Exception doesn't include iteration_index, use enumerate index
                    failed_iteration_indices.append(idx)
                    failed_iteration_errors[idx] = str(result)
                    continue
                if isinstance(result, tuple):
                    iteration_index, success = result
                    if success:
                        successful_iterations.append(iteration_index)
                    else:
                        failed_iteration_indices.append(iteration_index)
                        failed_iteration_errors[iteration_index] = "Iteration failed"

            if failed_iteration_indices:
                logger.warning(
                    "Loop execution: %d of %d parallel iterations failed. Results will contain None for failed iterations. Errors: %s",
                    len(failed_iteration_indices),
                    total_iterations,
                    failed_iteration_errors,
                )

            # Step 4: Extract parameter values from iterations BEFORE cleanup
            iteration_results = self.get_parameter_values_from_iterations(
                end_loop_node=end_loop_node,
                deserialized_flows=deserialized_flows,
                package_flow_result_success=package_result,
            )

            # Add None values for failed iterations that didn't produce any results
            # (e.g., nested loops may fail but still produce partial output)
            for failed_idx in failed_iteration_indices:
                if failed_idx not in iteration_results:
                    iteration_results[failed_idx] = None

            # Step 5: Extract last iteration values BEFORE cleanup (flows deleted in finally block)
            last_iteration_values = self.get_last_iteration_values_for_packaged_nodes(
                deserialized_flows=deserialized_flows,
                package_result=package_result,
                total_iterations=total_iterations,
            )

            return iteration_results, successful_iterations, last_iteration_values

        finally:
            # Step 5: Cleanup - delete all iteration flows
            # Suppress events during deletion to prevent sending them to websockets
            with EventSuppressionContext(event_manager, {DeleteFlowResultSuccess, DeleteFlowResultFailure}):
                for iteration_index, flow_name, _ in deserialized_flows:
                    delete_request = DeleteFlowRequest(flow_name=flow_name)
                    delete_result = await GriptapeNodes.ahandle_request(delete_request)
                    if not isinstance(delete_result, DeleteFlowResultSuccess):
                        logger.warning(
                            "Failed to delete iteration flow '%s' (iteration %d): %s",
                            flow_name,
                            iteration_index,
                            delete_result.result_details,
                        )

    async def _execute_loop_iterations_via_subprocess(  # noqa: PLR0913
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
        workflow_path: Path,
        workflow_result: Any,  # noqa: ARG002 - Used by wrapper methods for cleanup
        file_name_prefix: str,
        execution_type: str,
        *,
        run_sequentially: bool,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations via subprocess (unified helper for private/cloud execution).

        This unified helper handles both sequential and parallel execution modes for
        workflows that run as subprocesses (PRIVATE or CLOUD publishers).

        Args:
            package_result: The packaged flow with parameter mappings
            total_iterations: Number of iterations to run
            parameter_values_per_iteration: Dict mapping iteration_index -> parameter values
            end_loop_node: The End Loop Node to extract results for
            workflow_path: Path to the saved/published workflow file
            workflow_result: Result from saving/publishing the workflow
            file_name_prefix: Prefix for iteration-specific file names
            execution_type: Human-readable execution mode name for logging
            run_sequentially: If True, run iterations one-at-a-time; if False, run concurrently

        Returns:
            Tuple of (iteration_results, successful_iterations, last_iteration_values)
        """
        # if it's private execution, we aren't republishing it in a library.
        # So our original package is what is running, and we can count on using these mappings
        if execution_type == PRIVATE_EXECUTION:
            start_node_mapping = self.get_node_parameter_mappings(package_result, "start")
            start_node_name = start_node_mapping.node_name
        # For published libraries, we need to get the new Start Node name, based on what their registered nodes are.
        else:
            library = LibraryRegistry.get_library(execution_type)
            node_details = await self._get_workflow_start_end_nodes(library)
            start_node_type = node_details.start_flow_node_type
            node_metadata = library.get_node_metadata(start_node_type)
            start_node_name = node_metadata.display_name

        mode_str = "sequentially" if run_sequentially else "concurrently"
        logger.info(
            "Executing %d iterations %s in %s for loop '%s'",
            total_iterations,
            mode_str,
            execution_type,
            end_loop_node.name,
        )

        try:
            if run_sequentially:
                # Execute iterations one-at-a-time
                iteration_outputs: list[tuple[int, bool, dict[str, Any] | None]] = []
                for iteration_index in range(total_iterations):
                    try:
                        flow_input = {start_node_name: parameter_values_per_iteration[iteration_index]}
                        logger.info(
                            "Executing iteration %d/%d for loop '%s'",
                            iteration_index + 1,
                            total_iterations,
                            end_loop_node.name,
                        )

                        # Pass node for event updates if it's a SubflowNodeGroup (includes BaseIterativeNodeGroup)
                        subflow_node = end_loop_node if isinstance(end_loop_node, SubflowNodeGroup) else None
                        subprocess_result = await self._execute_subprocess(
                            published_workflow_filename=workflow_path,
                            file_name=f"{file_name_prefix}_iteration_{iteration_index}",
                            pickle_control_flow_result=True,
                            flow_input=flow_input,
                            node=subflow_node,
                        )
                        iteration_outputs.append((iteration_index, True, subprocess_result))
                    except Exception:
                        logger.exception("Iteration %d failed for loop '%s'", iteration_index, end_loop_node.name)
                        iteration_outputs.append((iteration_index, False, None))
            else:
                # Execute all iterations concurrently
                # Get subflow_node reference for event updates (scoped outside the closure)
                subflow_node = end_loop_node if isinstance(end_loop_node, SubflowNodeGroup) else None

                async def run_single_iteration(iteration_index: int) -> tuple[int, bool, dict[str, Any] | None]:
                    try:
                        flow_input = {start_node_name: parameter_values_per_iteration[iteration_index]}
                        logger.info(
                            "Executing iteration %d/%d for loop '%s'",
                            iteration_index + 1,
                            total_iterations,
                            end_loop_node.name,
                        )

                        subprocess_result = await self._execute_subprocess(
                            published_workflow_filename=workflow_path,
                            file_name=f"{file_name_prefix}_iteration_{iteration_index}",
                            pickle_control_flow_result=True,
                            flow_input=flow_input,
                            node=subflow_node,
                        )
                    except Exception:
                        logger.exception("Iteration %d failed for loop '%s'", iteration_index, end_loop_node.name)
                        return iteration_index, False, None
                    else:
                        return iteration_index, True, subprocess_result

                iteration_tasks = [run_single_iteration(i) for i in range(total_iterations)]
                iteration_outputs = await asyncio.gather(*iteration_tasks)

            # Extract results
            iteration_results, successful_iterations, last_iteration_values = (
                self._extract_iteration_results_from_subprocess(
                    iteration_outputs=iteration_outputs,
                    package_result=package_result,
                    end_loop_node=end_loop_node,
                )
            )

            logger.info(
                "Successfully completed %d/%d iterations %s in %s for loop '%s'",
                len(successful_iterations),
                total_iterations,
                mode_str,
                execution_type,
                end_loop_node.name,
            )

            return iteration_results, successful_iterations, last_iteration_values
        finally:
            # Cleanup handled by wrapper methods
            pass

    async def _execute_loop_iterations_sequentially_private(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations sequentially in private subprocesses (no cloud publishing)."""
        workflow_path, workflow_result = await self._save_workflow_file_for_loop(
            end_loop_node=end_loop_node,
            package_result=package_result,
            pickle_control_flow_result=True,
        )
        sanitized_loop_name = end_loop_node.name.replace(" ", "_")
        file_name_prefix = f"{sanitized_loop_name}_private_sequential_loop_flow"

        try:
            return await self._execute_loop_iterations_via_subprocess(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_loop_node,
                workflow_path=workflow_path,
                workflow_result=workflow_result,
                file_name_prefix=file_name_prefix,
                execution_type=PRIVATE_EXECUTION,
                run_sequentially=True,
            )
        finally:
            try:
                await self._delete_workflow(workflow_path=workflow_path)
            except Exception as e:
                logger.warning("Failed to cleanup workflow file: %s", e)

    async def _execute_loop_iterations_privately(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations in parallel via private subprocesses (no cloud publishing)."""
        workflow_path, workflow_result = await self._save_workflow_file_for_loop(
            end_loop_node=end_loop_node,
            package_result=package_result,
            pickle_control_flow_result=True,
        )
        sanitized_loop_name = end_loop_node.name.replace(" ", "_")
        file_name_prefix = f"{sanitized_loop_name}_private_loop_flow"

        try:
            return await self._execute_loop_iterations_via_subprocess(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_loop_node,
                workflow_path=workflow_path,
                workflow_result=workflow_result,
                file_name_prefix=file_name_prefix,
                execution_type=PRIVATE_EXECUTION,
                run_sequentially=False,
            )
        finally:
            try:
                await self._delete_workflow(workflow_path=workflow_path)
            except Exception as e:
                logger.warning("Failed to cleanup workflow file: %s", e)

    async def _save_workflow_file_for_loop(
        self,
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        *,
        pickle_control_flow_result: bool,
    ) -> tuple[Path, Any]:
        """Save workflow file for loop execution.

        Args:
            end_loop_node: The end loop node
            package_result: The packaged flow
            pickle_control_flow_result: Whether to pickle the control flow result

        Returns:
            Tuple of (workflow_path, workflow_result)
        """
        sanitized_loop_name = end_loop_node.name.replace(" ", "_")
        file_name = f"{sanitized_loop_name}_private_loop_flow"

        workflow_file_request = SaveWorkflowFileFromSerializedFlowRequest(
            file_name=file_name,
            serialized_flow_commands=package_result.serialized_flow_commands,
            workflow_shape=package_result.workflow_shape,
            pickle_control_flow_result=pickle_control_flow_result,
        )

        workflow_result = await GriptapeNodes.ahandle_request(workflow_file_request)
        if not isinstance(workflow_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
            msg = f"Failed to save workflow file for private loop execution: {workflow_result.result_details}"
            raise TypeError(msg)

        workflow_path = Path(workflow_result.file_path)
        logger.info("Saved workflow to '%s'", workflow_path)

        return workflow_path, workflow_result

    def _extract_iteration_results_from_subprocess(
        self,
        iteration_outputs: list[tuple[int, bool, dict[str, Any] | None]],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Extract results from subprocess iteration outputs.

        Args:
            iteration_outputs: List of (iteration_index, success, subprocess_result) tuples
            package_result: The packaged flow
            end_loop_node: The end loop node

        Returns:
            Tuple of (iteration_results, successful_iterations, last_iteration_values)
        """
        successful_iterations = []
        iteration_subprocess_outputs = {}

        for iteration_index, success, subprocess_result in iteration_outputs:
            if success and subprocess_result is not None:
                successful_iterations.append(iteration_index)
                iteration_subprocess_outputs[iteration_index] = subprocess_result

        # Extract the actual result values from subprocess outputs
        end_node_mapping = self.get_node_parameter_mappings(package_result, "end")
        end_node_param_mappings = end_node_mapping.parameter_mappings

        # Find which EndFlow parameter corresponds to new_item_to_add
        list_connections_request = ListConnectionsForNodeRequest(node_name=end_loop_node.name)
        list_connections_result = GriptapeNodes.handle_request(list_connections_request)

        endflow_param_name = None
        if isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            endflow_param_name = self._find_endflow_param_for_end_loop_node(
                list_connections_result.incoming_connections, end_node_param_mappings
            )

        # Extract iteration results from subprocess outputs
        iteration_results = {}
        for iteration_index in successful_iterations:
            subprocess_result = iteration_subprocess_outputs[iteration_index]
            parameter_output_values = self._extract_parameter_output_values(subprocess_result)

            if endflow_param_name and endflow_param_name in parameter_output_values:
                iteration_results[iteration_index] = parameter_output_values[endflow_param_name]

        # Get last iteration values from the last successful iteration
        last_iteration_values = {}
        if successful_iterations:
            last_iteration_index = max(successful_iterations)
            last_subprocess_result = iteration_subprocess_outputs[last_iteration_index]
            last_iteration_values = self._extract_parameter_output_values(last_subprocess_result)

        return iteration_results, successful_iterations, last_iteration_values

    async def _execute_loop_iterations_sequentially_via_publisher(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
        execution_type: str,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations sequentially via cloud publisher (Deadline Cloud, etc.)."""
        try:
            library = LibraryRegistry.get_library(name=execution_type)
        except KeyError:
            msg = f"Could not find library for execution environment {execution_type}"
            raise RuntimeError(msg)  # noqa: B904

        library_name = library.get_library_data().name
        sanitized_loop_name = end_loop_node.name.replace(" ", "_")
        file_name_prefix = f"{sanitized_loop_name}_{library_name.replace(' ', '_')}_sequential_loop_flow"

        # Pass node for publishing progress events if it's a SubflowNodeGroup
        publish_node = end_loop_node if isinstance(end_loop_node, SubflowNodeGroup) else None
        published_workflow_filename, workflow_result = await self._publish_workflow_for_loop_execution(
            package_result=package_result,
            library_name=library_name,
            file_name=file_name_prefix,
            node=publish_node,
        )

        try:
            return await self._execute_loop_iterations_via_subprocess(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_loop_node,
                workflow_path=Path(published_workflow_filename),
                workflow_result=workflow_result,
                file_name_prefix=file_name_prefix,
                execution_type=execution_type,
                run_sequentially=True,
            )
        finally:
            await self._cleanup_published_workflows(
                workflow_result=workflow_result,
                published_workflow_filename=published_workflow_filename,
            )

    async def _execute_loop_iterations_via_publisher(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        total_iterations: int,
        parameter_values_per_iteration: dict[int, dict[str, Any]],
        end_loop_node: BaseIterativeEndNode | BaseIterativeNodeGroup,
        execution_type: str,
    ) -> tuple[dict[int, Any], list[int], dict[str, Any]]:
        """Execute loop iterations in parallel via cloud publisher (Deadline Cloud, etc.)."""
        try:
            library = LibraryRegistry.get_library(name=execution_type)
        except KeyError:
            msg = f"Could not find library for execution environment {execution_type}"
            raise RuntimeError(msg)  # noqa: B904

        library_name = library.get_library_data().name
        sanitized_loop_name = end_loop_node.name.replace(" ", "_")
        file_name_prefix = f"{sanitized_loop_name}_{library_name.replace(' ', '_')}_loop_flow"

        # Pass node for publishing progress events if it's a SubflowNodeGroup
        publish_node = end_loop_node if isinstance(end_loop_node, SubflowNodeGroup) else None
        published_workflow_filename, workflow_result = await self._publish_workflow_for_loop_execution(
            package_result=package_result,
            library_name=library_name,
            file_name=file_name_prefix,
            node=publish_node,
        )

        try:
            return await self._execute_loop_iterations_via_subprocess(
                package_result=package_result,
                total_iterations=total_iterations,
                parameter_values_per_iteration=parameter_values_per_iteration,
                end_loop_node=end_loop_node,
                workflow_path=Path(published_workflow_filename),
                workflow_result=workflow_result,
                file_name_prefix=file_name_prefix,
                execution_type=library_name,
                run_sequentially=False,
            )
        finally:
            await self._cleanup_published_workflows(
                workflow_result=workflow_result,
                published_workflow_filename=published_workflow_filename,
            )

    async def _publish_workflow_for_loop_execution(
        self,
        package_result: PackageNodesAsSerializedFlowResultSuccess,
        library_name: str,
        file_name: str,
        node: BaseNode | None = None,
    ) -> tuple[Path, Any]:
        """Save and publish workflow for loop execution via publisher.

        Args:
            package_result: The packaged flow
            library_name: Name of the library to publish to
            file_name: Base file name for the workflow
            node: Optional node to receive publishing progress events

        Returns:
            Tuple of (published_workflow_filename, workflow_result)
        """
        workflow_file_request = SaveWorkflowFileFromSerializedFlowRequest(
            file_name=file_name,
            serialized_flow_commands=package_result.serialized_flow_commands,
            workflow_shape=package_result.workflow_shape,
            pickle_control_flow_result=True,
        )

        workflow_result = await GriptapeNodes.ahandle_request(workflow_file_request)
        if not isinstance(workflow_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
            msg = f"Failed to save workflow file for loop: {workflow_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004 - This is a runtime failure, not a type validation error

        # Publish to the library
        published_workflow_filename = await self._publish_library_workflow(
            workflow_result, library_name, file_name, node=node
        )

        logger.info("Successfully published workflow to '%s'", published_workflow_filename)

        return published_workflow_filename, workflow_result

    async def _cleanup_published_workflows(
        self,
        workflow_result: Any,
        published_workflow_filename: Path,
    ) -> None:
        """Clean up published workflow files.

        Args:
            workflow_result: The workflow result containing metadata
            published_workflow_filename: Path to the published workflow file
        """
        try:
            await self._delete_workflow(workflow_path=Path(workflow_result.file_path))
            await self._delete_workflow(workflow_path=published_workflow_filename)
        except Exception as e:
            logger.warning("Failed to cleanup workflow files: %s", e)

    def set_parameter_output_values_for_loops(
        self, subprocess_result: dict[str, dict[str | SerializedNodeCommands.UniqueParameterValueUUID, Any] | None]
    ) -> None:
        pass

    def _extract_parameter_output_values(
        self, subprocess_result: dict[str, dict[str | SerializedNodeCommands.UniqueParameterValueUUID, Any] | None]
    ) -> dict[str, Any]:
        """Extract and deserialize parameter output values from subprocess result.

        Returns:
            Dictionary of parameter names to their deserialized values
        """
        parameter_output_values = {}
        for result_dict in subprocess_result.values():
            # Handle backward compatibility: old flat structure
            if not isinstance(result_dict, dict) or "parameter_output_values" not in result_dict:
                parameter_output_values.update(result_dict)  # type: ignore[arg-type]
                continue

            param_output_vals = result_dict["parameter_output_values"]
            unique_uuid_to_values = result_dict.get("unique_parameter_uuid_to_values")

            # No UUID mapping - use values directly
            if not unique_uuid_to_values:
                parameter_output_values.update(param_output_vals)
                continue

            # Deserialize UUID-referenced values
            for param_name, param_value in param_output_vals.items():
                parameter_output_values[param_name] = self._deserialize_parameter_value(
                    param_name, param_value, unique_uuid_to_values
                )
        return parameter_output_values

    def _remove_packaged_nodes_from_queue(self, packaged_node_names: set[str]) -> None:
        """Remove nodes from global flow queue after they've been packaged for loop execution.

        When nodes are packaged for For Each loops, they will be deserialized into separate
        flow instances. We need to remove them from the global queue to prevent them from
        being executed in the main flow while also being copied into loop iterations.

        Args:
            packaged_node_names: Set of node names that were packaged
        """
        flow_manager = GriptapeNodes.FlowManager()
        node_manager = GriptapeNodes.NodeManager()

        # Get the nodes from the names
        packaged_nodes = set()
        for node_name in packaged_node_names:
            node = node_manager.get_node_by_name(node_name)
            if node:
                packaged_nodes.add(node)

        # Remove matching queue items from global queue
        items_to_remove = [item for item in flow_manager.global_flow_queue.queue if item.node in packaged_nodes]

        for item in items_to_remove:
            flow_manager.global_flow_queue.queue.remove(item)

        # Remove from DAG builder to prevent parallel execution in parent flow
        dag_builder = flow_manager.global_dag_builder
        if dag_builder:
            for node_name in packaged_node_names:
                # Remove from node_to_reference
                if node_name in dag_builder.node_to_reference:
                    dag_builder.node_to_reference.pop(node_name)

                # Remove from all networks and check if any become empty
                for network in list(dag_builder.graphs.values()):
                    if node_name in network.nodes():
                        network.remove_node(node_name)

    def _deserialize_parameter_value(self, param_name: str, param_value: Any, unique_uuid_to_values: dict) -> Any:
        """Deserialize a single parameter value, handling UUID references and pickling.

        Args:
            param_name: Parameter name for logging
            param_value: Either a direct value or UUID reference
            unique_uuid_to_values: Mapping of UUIDs to pickled values

        Returns:
            Deserialized parameter value
        """
        # Direct value (not a UUID reference)
        if param_value not in unique_uuid_to_values:
            return param_value

        stored_value = unique_uuid_to_values[param_value]
        try:
            return pickle.loads(stored_value)  # noqa: S301
        except pickle.UnpicklingError as e:
            logger.warning(
                "Attempted to unpickle stored value for parameter '%s'. Failed because: %s",
                param_name,
                e,
            )
            raise

    def _apply_parameter_values_to_node(
        self,
        node: BaseNode,
        parameter_output_values: dict[str, Any],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> None:
        """Apply deserialized parameter values back to the node.

        Sets parameter values on the node and updates parameter_output_values dictionary.
        Uses parameter_name_mappings from package_result to map packaged parameters back to original nodes.
        Works for both single-node and multi-node packages (SubflowNodeGroup).
        """
        # If the packaged flow fails, the End Flow Node in the library published workflow will have entered from 'failed'
        if "failed" in parameter_output_values and parameter_output_values["failed"] == CONTROL_INPUT_PARAMETER:
            msg = f"Failed to execute node: {node.name}, with exception: {parameter_output_values.get('result_details', 'No result details were returned.')}"
            raise RuntimeError(msg)

        # Use parameter mappings to apply values back to original nodes
        # Output values come from the End node (index 1 in the list)
        end_node_mapping = self.get_node_parameter_mappings(package_result, "end")
        end_node_param_mappings = end_node_mapping.parameter_mappings

        for param_name, param_value in parameter_output_values.items():
            # Check if this parameter has a mapping in the End node
            if param_name not in end_node_param_mappings:
                continue

            original_node_param = end_node_param_mappings[param_name]
            target_node_name = original_node_param.node_name
            target_param_name = original_node_param.parameter_name

            # Determine the target node - if this is a SubflowNodeGroup, look up the child node
            if isinstance(node, SubflowNodeGroup):
                if target_node_name not in node.nodes:
                    logger.warning(
                        "Node '%s' not found in SubflowNodeGroup '%s', skipping value application",
                        target_node_name,
                        node.name,
                    )
                    continue
                target_node = node.nodes[target_node_name]
            else:
                target_node = node

            # Get the parameter from the target node
            target_param = target_node.get_parameter_by_name(target_param_name)
            if target_param is None:
                logger.warning(
                    "Parameter '%s' not found on node '%s', skipping value application",
                    target_param_name,
                    target_node_name,
                )
                continue

            # Set the value on the target node
            # Provide source node/parameter to bypass connection conflict validation
            # These values are coming from execution results, treat as upstream values
            if target_param.type != ParameterTypeBuiltin.CONTROL_TYPE:
                GriptapeNodes.NodeManager().on_set_parameter_value_request(
                    SetParameterValueRequest(
                        node_name=target_node_name,
                        parameter_name=target_param_name,
                        value=param_value,
                        incoming_connection_source_node_name=node.name,
                        incoming_connection_source_parameter_name=target_param_name,
                    )
                )
            target_node.parameter_output_values[target_param_name] = param_value

            logger.debug(
                "Set parameter '%s' on node '%s' to value: %s",
                target_param_name,
                target_node_name,
                param_value,
            )

    def _apply_last_iteration_to_packaged_nodes(
        self,
        last_iteration_values: dict[str, Any],
        package_result: PackageNodesAsSerializedFlowResultSuccess,
    ) -> None:
        """Apply last iteration values to the original packaged nodes in main flow.

        After parallel loop execution, this sets the final state of each packaged node
        to match the last iteration's execution results. This is important for nodes that
        output values or produce artifacts during loop execution.

        Args:
            last_iteration_values: Dict mapping sanitized End node parameter names to values
            package_result: PackageNodesAsSerializedFlowResultSuccess containing parameter mappings and node names
        """
        if not last_iteration_values:
            logger.debug("No last iteration values to apply to packaged nodes")
            return

        # Get End node parameter mappings (index 1 in the list)
        end_node_mapping = self.get_node_parameter_mappings(package_result, "end")
        end_node_param_mappings = end_node_mapping.parameter_mappings

        node_manager = GriptapeNodes.NodeManager()

        # For each parameter in the End node, map it back to the original node and set the value
        for sanitized_param_name, param_value in last_iteration_values.items():
            # Check if this parameter has a mapping in the End node
            if sanitized_param_name not in end_node_param_mappings:
                continue

            original_node_param = end_node_param_mappings[sanitized_param_name]
            target_node_name = original_node_param.node_name
            target_param_name = original_node_param.parameter_name

            # Get the original packaged node in the main flow
            try:
                target_node = node_manager.get_node_by_name(target_node_name)
            except Exception:
                logger.warning(
                    "Could not find packaged node '%s' in main flow to apply last iteration values", target_node_name
                )
                continue

            # Get the parameter from the target node
            target_param = target_node.get_parameter_by_name(target_param_name)

            # Skip if parameter not found or is special parameter
            if target_param is None:
                logger.debug("Skipping missing parameter '%s' on node '%s'", target_param_name, target_node_name)
                continue

            # Skip control parameters
            if target_param.type == ParameterTypeBuiltin.CONTROL_TYPE:
                logger.debug("Skipping control parameter '%s' on node '%s'", target_param_name, target_node_name)
                continue

            # Set the value on the target node
            target_node.set_parameter_value(target_param_name, param_value)
            target_node.parameter_output_values[target_param_name] = param_value

            logger.debug(
                "Applied last iteration value to packaged node '%s' parameter '%s'",
                target_node_name,
                target_param_name,
            )

        logger.info(
            "Successfully applied %d parameter values from last iteration to packaged nodes",
            len(last_iteration_values),
        )

    async def _delete_workflow(self, workflow_path: Path) -> None:
        # Derive the registry key from the workflow path using workspace-relative logic so it
        # matches the key used during registration (push_workflow(file_path=__file__) in the workflow).
        workspace_path = await anyio.Path(GriptapeNodes.ConfigManager().workspace_path).resolve()
        resolved = await anyio.Path(workflow_path).resolve()
        if resolved.is_relative_to(workspace_path):
            path_for_key = str(resolved.relative_to(workspace_path))
        else:
            path_for_key = str(resolved)
        workflow_name = derive_registry_key(path_for_key)

        if not WorkflowRegistry.has_workflow_with_name(workflow_name):
            # Register the workflow so DeleteWorkflowRequest can find and remove it.
            # A subprocess may have registered it in its own process but not in the main process.
            load_workflow_metadata_request = LoadWorkflowMetadata(file_name=workflow_path.name)
            result = await GriptapeNodes.ahandle_request(load_workflow_metadata_request)
            if isinstance(result, LoadWorkflowMetadataResultSuccess):
                WorkflowRegistry.generate_new_workflow(
                    registry_key=workflow_name, metadata=result.metadata, file_path=path_for_key
                )

        delete_request = DeleteWorkflowRequest(name=workflow_name)
        delete_result = await GriptapeNodes.ahandle_request(delete_request)
        if isinstance(delete_result, DeleteWorkflowResultFailure):
            logger.error(
                "Failed to delete workflow '%s'. Error: %s",
                workflow_name,
                delete_result.result_details,
            )
        else:
            logger.info(
                "Cleanup result for workflow '%s': %s",
                workflow_name,
                delete_result.result_details,
            )

    async def _get_storage_backend(self) -> StorageBackend:
        storage_backend_str = GriptapeNodes.ConfigManager().get_config_value("storage_backend")
        # Convert string to StorageBackend enum
        try:
            storage_backend = StorageBackend(storage_backend_str)
        except ValueError:
            storage_backend = StorageBackend.LOCAL
        return storage_backend
