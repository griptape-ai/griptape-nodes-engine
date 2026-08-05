import inspect
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal, cast

from griptape_nodes.retained_mode.events.arbitrary_python_events import RunArbitraryPythonStringRequest
from griptape_nodes.retained_mode.events.base_events import (
    ResultPayload,
)
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetConfigSchemaRequest,
    GetConfigValueRequest,
    SetConfigCategoryRequest,
    SetConfigValueRequest,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
    ContinueExecutionStepRequest,
    GetFlowStateRequest,
    ResolveNodeRequest,
    SingleExecutionStepRequest,
    SingleNodeStepRequest,
    StartFlowRequest,
    UnresolveFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    DeleteFlowRequest,
    GetFlowMetadataRequest,
    ListFlowsInFlowRequest,
    ListNodesInFlowRequest,
    SetFlowMetadataRequest,
)
from griptape_nodes.retained_mode.events.library_events import (
    GetNodeMetadataFromLibraryRequest,
    ListNodeTypesInLibraryRequest,
    ListRegisteredLibrariesRequest,
)
from griptape_nodes.retained_mode.events.node_events import (
    BatchSetNodeLockStateRequest,
    BatchSetNodeLockStateResultFailure,
    BatchSetNodeLockStateResultSuccess,
    CreateNodeRequest,
    CreateNodeResultFailure,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    GetNodeMetadataRequest,
    GetNodeMetadataResultFailure,
    GetNodeMetadataResultSuccess,
    GetNodeResolutionStateRequest,
    ListParametersOnNodeRequest,
    ListParametersOnNodeResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultFailure,
    SetLockNodeStateResultSuccess,
    SetNodeMetadataRequest,
    SetNodeMetadataResultFailure,
    SetNodeMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import (
    RenameObjectRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AlterParameterDetailsRequest,
    ConversionConfig,
    GetConnectionsForParameterRequest,
    GetParameterDetailsRequest,
    GetParameterValueRequest,
    GetParameterValueResultFailure,
    GetParameterValueResultSuccess,
    MigrateParameterRequest,
    MigrateParameterResultFailure,
    MigrateParameterResultSuccess,
    RemoveParameterFromNodeRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    GetWorkflowRunCommandRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# Type alias for offset side values
OffsetSide = Literal["top_left", "top", "top_right", "right", "bottom_right", "bottom", "bottom_left", "left"]

MIN_NODES = 2

logger = logging.getLogger("griptpae_nodes_engine")


def node_param_split(node_and_param: str) -> tuple[str, str]:
    """Split a string in format 'node.param' into node and param.

    Splits on the final '.' so node names containing dots (e.g. "FLUX.2 Image Generation")
    are preserved intact; parameter names are identifiers and never contain dots.
    """
    if "." not in node_and_param:
        msg = f"Expected format 'node.param', got '{node_and_param}'"
        raise ValueError(msg)
    parts = node_and_param.rsplit(".", 1)
    return parts[0], parts[1]  # Explicitly return two values


def command_arg_handler(node_param_split_func: Callable) -> Callable:
    """Decorator to handle different argument patterns for commands.

    Allows either a positional string argument in format "node.param"
    or explicit keyword arguments (node, param).
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Skip first arg if it's the class (for @classmethod)
            instance_or_cls = args[0] if args else None
            args_to_process = args[1:] if args else []

            # Extract node and param information
            node = kwargs.get("node")
            param = kwargs.get("param")

            # Case 1: Direct string format as positional arg ("node.param")
            if args_to_process and isinstance(args_to_process[0], str):
                node_and_param = args_to_process[0]
                node_result, param_result = node_param_split_func(node_and_param)
                node = node_result  # Set the node
                param = param_result  # Set the param
                # Remove the processed arg
                args_to_process = args_to_process[1:]
            # Case 2: Explicit keyword args (already extracted)
            elif node is not None and param is not None:
                # We already have node and param from kwargs
                pass
            else:
                msg = (
                    "Either provide a string in format 'node.param' as the first argument "
                    "or both 'node' and 'param' as keyword arguments"
                )
                raise ValueError(msg)

            # Clean up kwargs by removing already processed arguments
            cleaned_kwargs = {k: v for k, v in kwargs.items() if k not in {"node", "param"}}

            # Call the original function with processed arguments
            if instance_or_cls is not None:
                return func(
                    instance_or_cls,
                    node=node,
                    param=param,
                    *args_to_process,  # noqa: B026
                    **cleaned_kwargs,
                )
            return func(node=node, param=param, *args_to_process, **cleaned_kwargs)  # noqa: B026

        return wrapper

    return decorator


class RetainedMode:
    # FLOW OPERATIONS
    @classmethod
    def create_flow(
        cls,
        flow_name: str | None = None,
        parent_flow_name: str | None = None,
    ) -> ResultPayload:
        """Creates a new flow within the Griptape Nodes system.

        Args:
            flow_name (str, optional): Name for the new flow. If not provided, a default name will be generated.
            parent_flow_name (str, optional): Name of the parent flow. If provided, the new flow will be created as a child flow.

        Returns:
            ResultPayload: Contains the result of the flow creation operation.

        Example:
            # Create a top-level flow
            result = cmd.create_flow("my_flow")

            # Create a child flow
            result = cmd.create_flow("child_flow", parent_flow_name="parent_flow")
        """
        request = CreateFlowRequest(parent_flow_name=parent_flow_name, flow_name=flow_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def delete_flow(cls, flow_name: str) -> ResultPayload:
        """Deletes an existing flow from the system.

        Args:
            flow_name (str): Name of the flow to delete.

        Returns:
            ResultPayload: Contains the result of the flow deletion operation.

        Example:
            # Delete a flow
            result = cmd.delete_flow("my_flow")
        """
        request = DeleteFlowRequest(flow_name=flow_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_flows(cls, parent_flow_name: str | None = None) -> ResultPayload:
        """Lists all flows within a parent flow or all top-level flows if no parent is specified.

        Args:
            parent_flow_name (str, optional): Name of the parent flow. If not provided, returns all top-level flows.

        Returns:
            ResultPayload: Contains a list of flow names.

        Example:
            # Get all top-level flows
            result = cmd.get_flows()

            # Get all child flows of a parent
            result = cmd.get_flows(parent_flow_name="parent_flow")
        """
        request = ListFlowsInFlowRequest(parent_flow_name=parent_flow_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_nodes_in_flow(cls, flow_name: str, node_types: list[str] | None = None) -> ResultPayload:
        """Lists all nodes contained within a specific flow.

        Args:
            flow_name (str): Name of the flow to inspect.
            node_types (list[str] | None): Optional list of Python class names to filter by
                (e.g. ["StartFlow", "Agent"]). None returns all nodes.

        Returns:
            ResultPayload: Contains a list of node names in the flow.

        Example:
            # Get all nodes in a flow
            result = cmd.get_nodes_in_flow("my_flow")

            # Get only Agent nodes
            result = cmd.get_nodes_in_flow("my_flow", node_types=["Agent"])
        """
        request = ListNodesInFlowRequest(flow_name=flow_name, node_types=node_types)
        result = GriptapeNodes().handle_request(request)
        return result

    # NODE OPERATIONS
    @classmethod
    def create_node(  # noqa: PLR0913
        cls,
        node_type: str,
        specific_library_name: str | None = None,
        node_name: str | None = None,
        parent_flow_name: str | None = None,
        metadata: dict[Any, Any] | None = None,
        parent_group_name: str | None = None,
    ) -> str | CreateNodeResultFailure:
        """Creates a node of the specified type and adds it to the current or a specified parent flow.

        Supports custom naming and metadata (e.g., UI position, display name, tags).

        Args:
            node_type (str): Type of node to create (e.g. "Agent", "Prompt", "MergeText").
            specific_library_name (str, optional): Library to search for the node type.
            node_name (str, optional): Custom name for the new node.
            parent_flow_name (str, optional): Parent flow to insert the node into (defaults to current).
            metadata (dict, optional): Extra node metadata such as {"position": {"x": 100, "y": 200"}}.
            parent_group_name (str, optional): Name of the group node to add this node to after creation.

        Returns:
            ResultPayload: Contains the name of the created node if successful.

        Example:
            agent = cmd.create_node("Agent")
            cmd.create_node("Prompt", node_name="intro_prompt")
            cmd.create_node("Agent", metadata={"position": {"x": 100, "y": 200"}})
        """
        request = CreateNodeRequest(
            node_name=node_name,
            node_type=node_type,
            specific_library_name=specific_library_name,
            override_parent_flow_name=parent_flow_name,
            metadata=metadata,
            parent_group_name=parent_group_name,
        )
        result = GriptapeNodes().handle_request(request)
        # Check if result is successful before accessing node_name
        if isinstance(result, CreateNodeResultSuccess):
            return result.node_name
        # You could return the result object for debugging
        logger.error("Failed to create node: %s", result)
        return cast("CreateNodeResultFailure", result)

    @classmethod
    def delete_node(
        cls,
        node_name: str,
    ) -> ResultPayload:
        """Deletes a node from the system.

        Args:
            node_name (str): Name of the node to delete.

        Returns:
            ResultPayload: Contains the result of the node deletion operation.

        Example:
            # Delete a node
            result = cmd.delete_node("my_node")
        """
        request = DeleteNodeRequest(node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_resolution_state_for_node(cls, node_name: str) -> ResultPayload:
        """Gets the current resolution state of a node.

        The resolution state indicates whether a node has been successfully resolved
        and is ready for execution.

        Args:
            node_name (str): Name of the node to check.

        Returns:
            ResultPayload: Contains the resolution state of the node.

        Example:
            # Check if a node is resolved
            result = cmd.get_resolution_state_for_node("my_node")
        """
        request = GetNodeResolutionStateRequest(node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_metadata_for_node(cls, node_name: str) -> ResultPayload:
        """Retrieves metadata associated with a node.

        Node metadata can include UI position, display name, tags, and other custom properties.

        Args:
            node_name (str): Name of the node to get metadata for.

        Returns:
            ResultPayload: Contains the node's metadata.

        Example:
            # Get node metadata
            result = cmd.get_metadata_for_node("my_node")
        """
        request = GetNodeMetadataRequest(node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def set_metadata_for_node(cls, node_name: str, metadata: dict[Any, Any]) -> ResultPayload:
        """Sets metadata for a node.

        Args:
            node_name (str): Name of the node to set metadata for.
            metadata (dict): Dictionary containing the metadata to set.

        Returns:
            ResultPayload: Contains the result of the metadata update operation.

        Example:
            # Set node position
            metadata = {
                "position": {"x": 100, "y": 200}
            }
            result = cmd.set_metadata_for_node("my_node", metadata)
        """
        request = SetNodeMetadataRequest(node_name=node_name, metadata=metadata)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def set_lock_node_state(
        cls, *, node_name: str | None = None, lock: bool = True
    ) -> SetLockNodeStateResultSuccess | SetLockNodeStateResultFailure:
        """Sets the lock state of a node.

        Args:
            node_name (str | None): Name of the node to lock/unlock. If None, uses the current context node.
            lock (bool): Whether to lock (True) or unlock (False) the node.

        Returns:
            ResultPayload: Contains the result of setting the node lock state.

        Example:
            # Lock a specific node
            result = cmd.set_lock_node_state("my_node", lock=True)

            # Unlock the current context node
            result = cmd.set_lock_node_state(lock=False)
        """
        request = SetLockNodeStateRequest(node_name=node_name, lock=lock)
        result = GriptapeNodes().handle_request(request)
        return cast("SetLockNodeStateResultSuccess | SetLockNodeStateResultFailure", result)

    @classmethod
    def batch_set_lock_node_state(
        cls, *, node_names: list[str], lock: bool = True
    ) -> BatchSetNodeLockStateResultSuccess | BatchSetNodeLockStateResultFailure:
        """Sets the lock state of multiple nodes.

        Args:
            node_names (list[str]): Names of nodes to lock/unlock.
            lock (bool): Whether to lock (True) or unlock (False) the nodes.
        """
        request = BatchSetNodeLockStateRequest(node_names=node_names, lock=lock)
        result = GriptapeNodes().handle_request(request)
        return cast("BatchSetNodeLockStateResultSuccess | BatchSetNodeLockStateResultFailure", result)

    @classmethod
    def get_connections_for_node(cls, node_name: str) -> ResultPayload:
        """Gets all connections associated with a node.

        This includes both incoming and outgoing connections to/from the node.

        Args:
            node_name (str): Name of the node to get connections for.

        Returns:
            ResultPayload: Contains a list of connections.

        Example:
            # Get all connections for a node
            result = cmd.get_connections_for_node("my_node")
        """
        request = ListConnectionsForNodeRequest(node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_connections_for_parameter(cls, parameter_name: str, node_name: str | None = None) -> ResultPayload:
        """Gets all connections associated with a specific parameter on a node.

        This includes both incoming and outgoing connections to/from the parameter.

        Args:
            parameter_name (str): Name of the parameter to get connections for.
            node_name (str | None): Name of the node containing the parameter. If None, uses current context.

        Returns:
            ResultPayload: Contains connection details for the parameter.

        Example:
            # Get connections for a parameter on a specific node
            result = cmd.get_connections_for_parameter("input_image", "my_node")

            # Get connections for a parameter on the current node
            result = cmd.get_connections_for_parameter("scale")
        """
        request = GetConnectionsForParameterRequest(parameter_name=parameter_name, node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def list_params(cls, node: str) -> list[str]:
        """Lists all parameters associated with a node.

        Args:
            node (str): Name of the node to list parameters for.

        Returns:
            list[str]: List of parameter names.

        Example:
            # List all parameters on a node
            params = cmd.list_params("my_node")
        """
        request = ListParametersOnNodeRequest(node_name=node)
        result = GriptapeNodes().handle_request(request)
        return cast("ListParametersOnNodeResultSuccess", result).parameter_names

    @classmethod
    def add_param(  # noqa: PLR0913
        cls,
        node_name: str,
        parameter_name: str,
        default_value: Any | None,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        edit: bool = False,  # noqa: FBT001, FBT002
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        ui_options: dict | None = None,
        mode_allowed_input: bool = True,  # noqa: FBT001, FBT002
        mode_allowed_property: bool = True,  # noqa: FBT001, FBT002
        mode_allowed_output: bool = True,  # noqa: FBT001, FBT002
        **kwargs,  # noqa: ARG003
    ) -> ResultPayload:
        """Adds or modifies a parameter on a node.

        This method can be used to either add a new parameter or modify an existing one.
        Parameters can have different modes (input, property, output) and can include
        tooltips and UI options.

        Args:
            node_name (str): Name of the node to add/modify the parameter on.
            parameter_name (str): Name of the parameter.
            default_value (Any, optional): Default value for the parameter.
            tooltip (str | list[dict]): Tooltip text or structured tooltip data.
            type (str, optional): Type of the parameter.
            input_types (list[str], optional): List of allowed input types.
            output_type (str, optional): Expected output type.
            edit (bool, optional): If True, modifies an existing parameter instead of creating new.
            tooltip_as_input (str | list[dict], optional): Tooltip specific to input mode.
            tooltip_as_property (str | list[dict], optional): Tooltip specific to property mode.
            tooltip_as_output (str | list[dict], optional): Tooltip specific to output mode.
            ui_options (dict, optional): Additional UI configuration options.
            mode_allowed_input (bool, optional): Whether parameter can be used as input.
            mode_allowed_property (bool, optional): Whether parameter can be used as property.
            mode_allowed_output (bool, optional): Whether parameter can be used as output.
            **kwargs: Additional keyword arguments that may be passed to the parameter creation/modification.

        Returns:
            ResultPayload: Contains the result of the parameter operation.

        Example:
            # Add a new parameter
            result = cmd.add_param(
                node_name="my_node",
                parameter_name="my_param",
                default_value="default",
                tooltip="My parameter tooltip",
                type="string"
            )

            # Modify an existing parameter
            result = cmd.add_param(
                node_name="my_node",
                parameter_name="my_param",
                default_value="new_default",
                tooltip="Updated tooltip",
                edit=True
            )
        """
        if edit:
            request = AlterParameterDetailsRequest(
                parameter_name=parameter_name,
                node_name=node_name,
                default_value=default_value,
                tooltip=tooltip,
                type=type,
                input_types=input_types,
                output_type=output_type,
                tooltip_as_input=tooltip_as_input,
                tooltip_as_property=tooltip_as_property,
                tooltip_as_output=tooltip_as_output,
                mode_allowed_input=mode_allowed_input,
                mode_allowed_property=mode_allowed_property,
                mode_allowed_output=mode_allowed_output,
                ui_options=ui_options,
            )
        else:
            request = AddParameterToNodeRequest(
                parameter_name=parameter_name,
                node_name=node_name,
                default_value=default_value,
                tooltip=tooltip,
                type=type,
                input_types=input_types,
                output_type=output_type,
                tooltip_as_input=tooltip_as_input,
                tooltip_as_property=tooltip_as_property,
                tooltip_as_output=tooltip_as_output,
                mode_allowed_input=mode_allowed_input,
                mode_allowed_property=mode_allowed_property,
                mode_allowed_output=mode_allowed_output,
                ui_options=ui_options,
            )
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def del_param(cls, node_name: str, parameter_name: str) -> ResultPayload:
        """Removes a parameter from a node.

        Args:
            node_name (str): Name of the node containing the parameter.
            parameter_name (str): Name of the parameter to remove.

        Returns:
            ResultPayload: Contains the result of the parameter deletion operation.

        Example:
            # Remove a parameter
            result = cmd.del_param("my_node", "my_param")
        """
        request = RemoveParameterFromNodeRequest(parameter_name=parameter_name, node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def create_node_relative_to(  # noqa: C901, PLR0911, PLR0913
        cls,
        reference_node_name: str,
        new_node_type: str,
        new_node_name: str | None = None,
        specific_library_name: str | None = None,
        offset_side: OffsetSide = "right",
        offset_x: int = 0,
        offset_y: int = 0,
        *,
        swap: bool = False,
        lock: bool = True,
        match_size: bool = False,
    ) -> (
        str
        | GetNodeMetadataResultFailure
        | CreateNodeResultFailure
        | SetNodeMetadataResultFailure
        | SetLockNodeStateResultFailure
    ):
        """Create a new node positioned relative to an existing node.

        Args:
            reference_node_name: Name of the existing node to position relative to
            new_node_type: Type of the new node to create
            new_node_name: Name for the new node (optional, will be generated if not provided)
            specific_library_name: Specific library to use for the new node
            offset_side: Reference side/position from reference node (OffsetSide)
                        - "top_left", "top_right", "bottom_left", "bottom_right": corner positions
                        - "top", "bottom", "left", "right": midpoint positions
                        - Invalid values default to "right"
            offset_x: Horizontal offset (behavior depends on swap):
                      - When swap=False: pixels (negative = left, positive = right)
                      - When swap=True: additional pixel spacing between nodes (negative = closer, positive = further)
            offset_y: Vertical offset (behavior depends on swap):
                      - When swap=False: pixels (negative = up, positive = down)
                      - When swap=True: additional pixel spacing between nodes (negative = closer, positive = further)
            swap: If True, create new node at reference position and move reference node relative to new node.
                  Also changes offset_x/offset_y to be additional pixel spacing between nodes instead of absolute position offsets.
            lock: If True, lock the old node
            match_size: If True, the new node will have the same width and height as the reference node,
                       ensuring consistent and predictable spacing calculations. Defaults to False.

        Returns:
            String node name if successful, ResultPayload if failed
        """
        # Get the reference node's metadata
        get_metadata_result = GriptapeNodes().handle_request(GetNodeMetadataRequest(node_name=reference_node_name))
        if not isinstance(get_metadata_result, GetNodeMetadataResultSuccess):
            msg = f"{reference_node_name}: Failed to get reference node's metadata: {get_metadata_result}"
            logger.warning(msg)
            return cast("GetNodeMetadataResultFailure", get_metadata_result)

        reference_metadata = get_metadata_result.metadata
        reference_position = reference_metadata.get("position", {"x": 0, "y": 0})
        reference_size = reference_metadata.get("size", {"width": 200, "height": 100})

        # Detect if the reference node is in a group so the new node joins the same group
        parent_group_name = None
        reference_node = GriptapeNodes.ObjectManager().attempt_get_object_by_name(reference_node_name)
        if (
            reference_node is not None
            and hasattr(reference_node, "parent_group")
            and reference_node.parent_group is not None
        ):
            parent_group_name = reference_node.parent_group.name

        # Create the new node, optionally with matching size from reference node
        metadata = None
        if match_size:
            metadata = {"size": reference_size}
        create_result = cls.create_node(
            node_type=new_node_type,
            node_name=new_node_name,
            specific_library_name=specific_library_name,
            metadata=metadata,
            parent_group_name=parent_group_name,
        )

        # Check if creation succeeded, create_node returns the node name if successful
        if isinstance(create_result, str):
            new_node_name = create_result
        else:
            return create_result

        # Calculate position based on offset_side and offsets
        new_position = cls._calculate_relative_position(
            reference_position, reference_size, offset_side, offset_x, offset_y, swap=swap
        )

        if swap:
            # Swap mode: Create new node at reference position, move reference node relative to new node
            new_position = reference_position
            reference_new_position = cls._calculate_relative_position(
                new_position, reference_size, offset_side, offset_x, offset_y, swap=swap
            )

            # Set the new node's position (at reference position)
            set_metadata_result = GriptapeNodes().handle_request(
                SetNodeMetadataRequest(node_name=new_node_name, metadata={"position": new_position})
            )
            if not isinstance(set_metadata_result, SetNodeMetadataResultSuccess):
                msg = f"{reference_node_name} -> {new_node_name}: Failed to set new node's position: {set_metadata_result}"
                logger.warning(msg)
                return cast("SetNodeMetadataResultFailure", set_metadata_result)

            # Move the reference node to its new position
            set_reference_metadata_result = GriptapeNodes().handle_request(
                SetNodeMetadataRequest(node_name=reference_node_name, metadata={"position": reference_new_position})
            )
            if not isinstance(set_reference_metadata_result, SetNodeMetadataResultSuccess):
                msg = f"{reference_node_name} -> {new_node_name}: Failed to set reference node's position: {set_reference_metadata_result}"
                logger.warning(msg)
                return cast("SetNodeMetadataResultFailure", set_reference_metadata_result)

        else:
            # Normal mode: Create new node relative to reference node
            new_position = cls._calculate_relative_position(
                reference_position, reference_size, offset_side, offset_x, offset_y, swap=swap
            )

            # Set the new node's position
            set_metadata_result = GriptapeNodes().handle_request(
                SetNodeMetadataRequest(node_name=new_node_name, metadata={"position": new_position})
            )
            if not isinstance(set_metadata_result, SetNodeMetadataResultSuccess):
                msg = f"{reference_node_name} -> {new_node_name}: Failed to set new node's position: {set_metadata_result}"
                logger.warning(msg)
                return cast("SetNodeMetadataResultFailure", set_metadata_result)

        if lock:
            set_lock_node_state_result = cls.set_lock_node_state(node_name=reference_node_name, lock=True)
            if not isinstance(set_lock_node_state_result, SetLockNodeStateResultSuccess):
                msg = f"{reference_node_name}: Failed to lock reference node: {set_lock_node_state_result}"
                logger.warning(msg)
                return set_lock_node_state_result
        return new_node_name

    @classmethod
    def _calculate_relative_position(  # noqa: PLR0913
        cls,
        base_position: dict,
        base_size: dict,
        offset_side: OffsetSide,
        offset_x: int,
        offset_y: int,
        *,
        swap: bool,
    ) -> dict:
        """Calculate position based on offset_side and offsets."""
        if swap:
            # For swap mode, offset_x/offset_y are additional pixel spacing between nodes
            # Formula: reference_position - reference_size + offset  # noqa: ERA001
            x_offset = base_position["x"] - base_size["width"] + offset_x
            y_offset = base_position["y"] - base_size["height"] + offset_y
            x_center = base_position["x"] - base_size["width"] // 2 + offset_x
            y_center = base_position["y"] - base_size["height"] // 2 + offset_y
            x_right = base_position["x"] + offset_x
            y_bottom = base_position["y"] + offset_y
        else:
            # Absolute pixel offsets (normal mode)
            effective_offset_x = offset_x
            effective_offset_y = offset_y
            # Calculate base offsets for each side (relative to reference node position)
            x_offset = base_position["x"] + effective_offset_x
            y_offset = base_position["y"] + effective_offset_y
            x_center = base_position["x"] + base_size["width"] // 2 + effective_offset_x
            y_center = base_position["y"] + base_size["height"] // 2 + effective_offset_y
            x_right = base_position["x"] + base_size["width"] + effective_offset_x
            y_bottom = base_position["y"] + base_size["height"] + effective_offset_y

        # Position mapping
        positions = {
            "top_left": {"x": x_offset, "y": y_offset},
            "top": {"x": x_center, "y": y_offset},
            "top_right": {"x": x_right, "y": y_offset},
            "right": {"x": x_right, "y": y_center},
            "bottom_right": {"x": x_right, "y": y_bottom},
            "bottom": {"x": x_center, "y": y_bottom},
            "bottom_left": {"x": x_offset, "y": y_bottom},
            "left": {"x": x_offset, "y": y_center},
        }

        return positions.get(offset_side, positions["right"])  # Default to right

    @classmethod
    def migrate_parameter(  # noqa: PLR0913
        cls,
        source_node_name: str,
        target_node_name: str,
        source_parameter_name: str,
        target_parameter_name: str,
        input_conversion: ConversionConfig | None = None,
        output_conversion: ConversionConfig | None = None,
        value_transform: Callable | None = None,
        *,
        break_connections: bool = True,
    ) -> MigrateParameterResultSuccess | MigrateParameterResultFailure:
        """Migrate a parameter from one node to another with optional conversions.

        This command handles:
        - Direct parameter renaming and connection migration
        - Value transformation when no incoming connections exist
        - Creation of intermediate conversion nodes for complex type conversions on incoming or outgoing connections
        - Multiple incoming and outgoing connections (including execution parameters)

        Args:
            source_node_name (str): Name of the source node.
            target_node_name (str): Name of the target node.
            source_parameter_name (str): Name of the parameter to migrate from.
            target_parameter_name (str): Name of the parameter to migrate to.
            input_conversion (ConversionConfig, optional): Configuration for converting incoming connections.
            output_conversion (ConversionConfig, optional): Configuration for converting outgoing connections.
            value_transform (Callable, optional): Function to transform values when no incoming connections exist.
            break_connections (bool, optional): If True, break any existing connections for the original parameter
        Returns:
            ResultPayload: Contains the result of the migration operation.

        Example:
            # Simple parameter migration
            result = cmd.migrate_parameter("old_node", "new_node", "input_image", "input_image")

            # Parameter migration with conversion
            result = cmd.migrate_parameter(
                "old_node", "new_node", "scale", "percentage_scale",
                input_conversion=ConversionConfig(
                    library="Griptape Nodes Library",
                    node_type="Math",
                    input_parameter="A",
                    output_parameter="result",
                    additional_parameters={
                        "operation": "multiply [A * B]",
                        "B": 100
                    },
                    offset_x=-300
                ),
                output_conversion=ConversionConfig(
                    library="Griptape Nodes Library",
                    node_type="Math",
                    input_parameter="A",
                    output_parameter="result",
                    additional_parameters={
                        "operation": "divide [A / B]",
                        "B": 100
                    },
                    offset_x=50
                ),
                value_transform=lambda x: x * 100
            )
        """
        request = MigrateParameterRequest(
            source_node_name=source_node_name,
            target_node_name=target_node_name,
            source_parameter_name=source_parameter_name,
            target_parameter_name=target_parameter_name,
            input_conversion=input_conversion,
            output_conversion=output_conversion,
            value_transform=value_transform,
            break_connections=break_connections,
        )
        result = GriptapeNodes().handle_request(request)
        return cast("MigrateParameterResultSuccess | MigrateParameterResultFailure", result)

    @classmethod
    @command_arg_handler(node_param_split_func=node_param_split)
    def param_info(cls, *, node: str, param: str, **kwargs) -> Any:  # noqa: ARG003
        """Gets detailed information about a parameter.

        Args:
            node (str): Name of the node containing the parameter.
            param (str): Name of the parameter to get info for.
            **kwargs: Additional arguments.

        Returns:
            Any: Contains detailed parameter information.

        Example:
            # Get parameter info using node.param format
            info = cmd.param_info("my_node.my_param")

            # Get parameter info using keyword arguments
            info = cmd.param_info(node="my_node", param="my_param")
        """
        request = GetParameterDetailsRequest(parameter_name=param, node_name=node)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def exists(cls, node: str) -> bool:
        rsl = GriptapeNodes.ObjectManager()
        return node in rsl.get_filtered_subset()

    @classmethod
    def get_list_index_from_param_str(cls, param_name: str) -> tuple[str, int | None]:
        index = None
        final_param_name = param_name
        # Check if we're trying to get a list index using syntax like "param[0]"
        if "[" in param_name and param_name.endswith("]"):
            try:
                # Extract the base parameter name and the index
                base_param_name, index_str = param_name.split("[", 1)
                index = int(index_str[:-1])  # Remove the closing ']' and convert to int
                final_param_name = base_param_name
            except Exception as e:
                details = f"Invalid list index format in parameter name: '{param_name}'. Error: {e}."
                logger.exception(details)
        return (final_param_name, index)

    @classmethod
    def parse_indexed_variable(cls, expr_str: str) -> tuple[str, list[str]]:
        """Parse an indexed variable expression and return the variable name and a list of index operations.

        Args:
            expr_str (str): The expression to parse (e.g., "my_value[2][3]" or "123_var['key']")

        Returns:
            tuple: (variable_name, list_of_indices)

        Examples:
            parse_indexed_variable("my_value[2][3]")
            # Returns: ("my_value", ["2", "3"])

            parse_indexed_variable("123var['key'][0]")
            # Returns: ("123var", ["'key'", "0"])
        """
        import re

        # Find the first opening bracket to separate the variable name from indexing operations
        bracket_match = re.search(r"[\[\{]", expr_str)

        if bracket_match:
            # There are indexing operations
            first_bracket_pos = bracket_match.start()
            var_name = expr_str[:first_bracket_pos]
            remaining = expr_str[first_bracket_pos:]
        else:
            # No indexing operations
            var_name = expr_str
            remaining = ""

        # Extract all index operations
        index_pattern = r"\[(.*?)\]"
        indices = re.findall(index_pattern, remaining)

        return var_name, indices

    @classmethod
    def _get_indexed_value(cls, node_name: str, base_param_name: str, indices: list) -> Any:
        """Get a value at specified indices from a container parameter.

        Args:
            node_name: Name of the node containing the parameter
            base_param_name: Base name of the parameter (without indices)
            indices: List of indices to navigate through

        Returns:
            tuple: (success, value_or_error) where success is a boolean and
                value_or_error is either the retrieved value or an error result
        """
        # Get the container value
        request = GetParameterValueRequest(
            parameter_name=base_param_name,
            node_name=node_name,
        )
        result = GriptapeNodes().handle_request(request)

        if result.failed():
            return False, result

        # Navigate through indices
        result = cast("GetParameterValueResultSuccess", result)
        curr_value = result.value
        for idx_or_key in indices:
            if isinstance(curr_value, list):
                # Convert index to int if needed
                try:
                    idx = int(idx_or_key) if not isinstance(idx_or_key, int) else idx_or_key
                except ValueError:
                    error_msg = f"Failed on key/index '{idx_or_key}'. Int required."
                    return False, error_msg

                # Check if index is in range
                if idx < 0 or idx >= len(curr_value):
                    error_msg = f"Failed on key/index '{idx_or_key}' because it was out of range. Object had {len(curr_value)} elements."
                    return False, error_msg

                curr_value = curr_value[idx]
            else:
                error_msg = f"Failed on key/index '{idx_or_key}' because container was a type that was not expected."
                return False, error_msg

        return True, curr_value

    @classmethod
    def _set_indexed_value(cls, node_name: str, base_param_name: str, indices: list, value: Any) -> ResultPayload:
        """Set a value at specified indices in a container parameter.

        Args:
            node_name: Name of the node containing the parameter
            base_param_name: Base name of the parameter (without indices)
            indices: List of indices to navigate through
            value: Value to set at the specified location

        Returns:
            ResultPayload: Result of the operation
        """
        # If no indices, set directly
        if not indices:
            request = SetParameterValueRequest(
                parameter_name=base_param_name,
                node_name=node_name,
                value=value,
            )
            return GriptapeNodes().handle_request(request)

        # Get the container value
        request = GetParameterValueRequest(
            parameter_name=base_param_name,
            node_name=node_name,
        )
        result = GriptapeNodes().handle_request(request)

        if result.failed():
            return result

        # Navigate to the proper location and set the value
        result = cast("GetParameterValueResultSuccess", result)
        container = result.value
        curr = container

        for index_ctr, idx_or_key in enumerate(indices):
            if isinstance(curr, list):
                # Convert index to int
                try:
                    idx = int(idx_or_key)
                except ValueError:
                    error_msg = f"Failed on key/index '{idx_or_key}' because it wasn't an int as expected."
                    logger.error(error_msg)
                    return GetParameterValueResultFailure(result_details=error_msg)

                # Handle negative indices
                if idx < 0:
                    error_msg = f"Failed on key/index '{idx_or_key}' because it was less than zero."
                    logger.error(error_msg)
                    return GetParameterValueResultFailure(result_details=error_msg)

                # Extend the list if needed
                while len(curr) <= idx:
                    curr.append(None)

                # If at the final index, set the value
                if index_ctr == len(indices) - 1:
                    curr[idx] = value
                else:
                    # Move to the next level
                    curr = curr[idx]
            else:
                error_msg = f"Failed on key/index '{idx_or_key}' because it was a type that was not expected."
                logger.error(error_msg)
                return GetParameterValueResultFailure(result_details=error_msg)

        # Update the container
        set_request = SetParameterValueRequest(
            parameter_name=base_param_name,
            node_name=node_name,
            value=container,
        )
        return GriptapeNodes().handle_request(set_request)

    @classmethod
    def get_value(cls, *args, **kwargs) -> Any:
        """Gets the value of a parameter on a node.

        This method supports both direct parameter access and indexed access for list/array parameters.
        The parameter can be specified either as a single string argument ("node.param") or as
        keyword arguments (node="node", param="param").

        Args:
            *args: Optional positional arguments. If provided, first argument should be "node.param".
            **kwargs: Keyword arguments:
                node (str): Name of the node containing the parameter.
                param (str): Name of the parameter to get value from.

        Returns:
            Any: The value of the parameter, or a failure result if the operation failed.

        Example:
            # Get value using node.param format
            value = cmd.get_value("my_node.my_param")

            # Get value using keyword arguments
            value = cmd.get_value(node="my_node", param="my_param")

            # Get value from a list parameter using indexing
            value = cmd.get_value("my_node.my_list[0]")
        """
        node = kwargs.pop("node", None)
        param = kwargs.pop("param", None)
        lrg = len(args)
        if lrg > 0:
            node, param = node_param_split(args[0])

        # Chop up the param name to see if there are any indices/keys in there.
        base_param_name, indices = cls.parse_indexed_variable(param)

        request = GetParameterValueRequest(
            parameter_name=base_param_name,
            node_name=node,
        )
        result = GriptapeNodes().handle_request(request)

        if result.succeeded():
            # Now see if there were any indices specified.
            result = cast("GetParameterValueResultSuccess", result)
            curr_pos_value = result.value
            for idx_or_key in indices:
                # What is the type of the current object in the chain?
                if isinstance(curr_pos_value, list):
                    # Index better be an int.
                    if not isinstance(idx_or_key, int):
                        error_msg = f"get_value failed for {node}.{param} on key/index {idx_or_key} only ints allowed."
                        logger.error(error_msg)
                        return GetParameterValueResultFailure(result_details=error_msg)
                    # Is the index in range?
                    if (idx_or_key < 0) or (idx_or_key >= len(curr_pos_value)):
                        error_msg = f"get_value failed for {node}.{param} on key/index {idx_or_key} out of range."
                        logger.error(error_msg)
                        return GetParameterValueResultFailure(result_details=error_msg)
                    curr_pos_value = curr_pos_value[idx_or_key]
                else:
                    error_msg = f"get_value failed for {node}.{param} on key/index {idx_or_key} because it was a type that was not expected."
                    logger.error(error_msg)
                    return GetParameterValueResultFailure(result_details=error_msg)
            # All done
            return curr_pos_value
        return result

    @classmethod
    def set_value(cls, *args, **kwargs) -> Any:  # noqa: C901, PLR0912
        """Sets the value of a parameter on a node.

        This method supports both direct parameter access and indexed access for list/array parameters.
        The parameter can be specified either as a single string argument ("node.param") or as
        keyword arguments (node="node", param="param"). The value to set can be provided as
        the second positional argument or as a keyword argument (value=value).

        Args:
            *args: Optional positional arguments:
                - First argument can be "node.param" format
                - Second argument can be the value to set
            **kwargs: Keyword arguments:
                node (str): Name of the node containing the parameter.
                param (str): Name of the parameter to set value for.
                value (Any): Value to set for the parameter.

        Returns:
            Any: Result of the set operation.

        Example:
            # Set value using node.param format
            result = cmd.set_value("my_node.my_param", "new_value")

            # Set value using keyword arguments
            result = cmd.set_value(node="my_node", param="my_param", value="new_value")

            # Set value in a list parameter using indexing
            result = cmd.set_value("my_node.my_list[0]", "new_value")

            # Set value in a nested list
            result = cmd.set_value("my_node.my_list[0][1]", "new_value")
        """
        node = kwargs.pop("node", None)
        param = kwargs.pop("param", None)
        value = kwargs.pop("value", None)

        lrg = len(args)
        if lrg > 0:
            node, param = node_param_split(args[0])
        if lrg > 1 and value is None:
            value = args[1]

        if not node or not param or value is None:
            msg = (
                "Missing required parameters. Use one of these formats:\n"
                '  set_value("node.param", value)\n'
                '  set_value("node.param", value=value)\n'
                '  set_value(node="node", param="param", value=value)'
            )
            raise ValueError(msg)

        # Chop up the param name to see if there are any indices/keys in there.
        base_param_name, indices = cls.parse_indexed_variable(param)

        # If we have no indices, set the value directly.
        if len(indices) == 0:
            request = SetParameterValueRequest(
                parameter_name=base_param_name,
                node_name=node,
                value=value,
            )
            result = GriptapeNodes().handle_request(request)
            logger.info("\nD:%s", f"{result=}")
        else:
            # We have indices. Get the value of the container first, then attempt to move all the way up to the end.
            request = GetParameterValueRequest(
                parameter_name=base_param_name,
                node_name=node,
            )
            result = GriptapeNodes().handle_request(request)

            if result.failed():
                logger.error(
                    'set_value failed for "%s.%s", failed to get value for container "%s".',
                    node,
                    param,
                    base_param_name,
                )
                return result

            base_container = cast("GetParameterValueResultSuccess", result).value
            # Start progress at the base
            curr_pos_value = base_container
            for index_ctr, idx_or_key in enumerate(indices):
                # What is the type of the current object in the chain?
                if isinstance(curr_pos_value, list):
                    # Index better be an int.
                    try:
                        idx_or_key_as_int = int(idx_or_key)
                    except ValueError:
                        error_msg = (
                            f'set_value for "{node}.{param}", failed on key/index "{idx_or_key}". Requires an int.'
                        )
                        logger.exception(error_msg)
                        return GetParameterValueResultFailure(result_details=error_msg)
                    # Is the index in range?
                    if idx_or_key_as_int < 0:
                        error_msg = f'set_value for "{node}.{param}", failed on key/index "{idx_or_key}" because it was less than zero.'
                        logger.error(error_msg)
                        return GetParameterValueResultFailure(result_details=error_msg)
                    # Extend the list if needed to accommodate the index.
                    while len(curr_pos_value) <= idx_or_key_as_int:
                        curr_pos_value.append(None)

                    # If we're at the end, assign the value.
                    if index_ctr == len(indices) - 1:
                        curr_pos_value[idx_or_key_as_int] = value

                        # Actually attempt to set the value, which should do type validation, etc.
                        request = SetParameterValueRequest(
                            parameter_name=base_param_name,
                            node_name=node,
                            value=base_container,  # Re-assign the entire updated container.
                        )
                        result = GriptapeNodes().handle_request(request)
                        return result
                    # Advance.
                    curr_pos_value = curr_pos_value[idx_or_key_as_int]
                else:
                    error_msg = f'set_value on "{node}.{param}" failed on key/index "{idx_or_key}" because it was a type that was not expected.'
                    logger.error(error_msg)
                    return GetParameterValueResultFailure(result_details=error_msg)
            # All done
        return result

    @classmethod
    def connect(cls, source: str, destination: str) -> ResultPayload:
        """Creates a connection between two node parameters.

        Args:
            source (str): Source node and parameter in format "node.param".
            destination (str): Destination node and parameter in format "node.param".

        Returns:
            ResultPayload: Contains the result of the connection operation.

        Example:
            # Connect output of one node to input of another
            result = cmd.connect("source_node.output", "target_node.input")
        """
        src_node, src_param = node_param_split(source)
        dst_node, dst_param = node_param_split(destination)

        request = CreateConnectionRequest(
            source_node_name=src_node,
            source_parameter_name=src_param,
            target_node_name=dst_node,
            target_parameter_name=dst_param,
        )
        return GriptapeNodes().handle_request(request)

    @classmethod
    def exec_chain(cls, *node_names) -> dict:
        """Creates a chain of execution connections between nodes.

        This method creates connections between nodes in sequence, connecting
        the exec_out of each node to the exec_in of the next node.

        Args:
            *node_names: Variable number of node names to chain together.

        Returns:
            dict: Dictionary containing results of each connection attempt.

        Example:
            # Create an execution chain of three nodes
            results = cmd.exec_chain("node1", "node2", "node3")
        """
        results = {}
        failures = []

        # Need at least 2 nodes to make a connection
        if len(node_names) < MIN_NODES:
            return {"error": "Need at least 2 nodes to create a chain"}

        # Create connections between consecutive nodes
        for i in range(len(node_names) - 1):
            source_node = node_names[i]
            target_node = node_names[i + 1]

            request = CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name="exec_out",
                target_node_name=target_node,
                target_parameter_name="exec_in",
            )

            result = GriptapeNodes().handle_request(request)
            results[f"{source_node} -> {target_node}"] = result

            # Track failures without halting execution
            if not hasattr(result, "success") or not getattr(result, "success", False):
                failures.append(f"{source_node} -> {target_node}")

        # Add summary of failures to the results
        if failures:
            results["failures"] = failures

        return results

    @classmethod
    def delete_connection(
        cls,
        source_node_name: str,
        source_param_name: str,
        target_node_name: str,
        target_param_name: str,
    ) -> ResultPayload:
        """Deletes a connection between two node parameters.

        Args:
            source_node_name (str): Name of the source node.
            source_param_name (str): Name of the source parameter.
            target_node_name (str): Name of the target node.
            target_param_name (str): Name of the target parameter.

        Returns:
            ResultPayload: Contains the result of the connection deletion operation.

        Example:
            # Delete a connection between nodes
            result = cmd.delete_connection(
                "source_node",
                "output",
                "target_node",
                "input"
            )
        """
        request = DeleteConnectionRequest(
            source_node_name=source_node_name,
            source_parameter_name=source_param_name,
            target_node_name=target_node_name,
            target_parameter_name=target_param_name,
        )
        result = GriptapeNodes().handle_request(request)
        return result

    # LIBRARY OPERATIONS
    @classmethod
    def get_available_libraries(cls) -> ResultPayload:
        """Gets a list of all available node libraries.

        Returns:
            ResultPayload: Contains a list of library names.

        Example:
            # Get all available libraries
            libraries = cmd.get_available_libraries()
        """
        request = ListRegisteredLibrariesRequest()
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_node_types_in_library(cls, library_name: str) -> ResultPayload:
        """Gets a list of all node types available in a specific library.

        Args:
            library_name (str): Name of the library to inspect.

        Returns:
            ResultPayload: Contains a list of node types.

        Example:
            # Get all node types in a library
            node_types = cmd.get_node_types_in_library("my_library")
        """
        request = ListNodeTypesInLibraryRequest(library=library_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_node_metadata_from_library(cls, library_name: str, node_type_name: str) -> ResultPayload:
        """Gets metadata for a specific node type from a library.

        This includes information about the node's parameters, capabilities,
        and other configuration options.

        Args:
            library_name (str): Name of the library containing the node type.
            node_type_name (str): Name of the node type to get metadata for.

        Returns:
            ResultPayload: Contains the node type's metadata.

        Example:
            # Get metadata for a specific node type
            metadata = cmd.get_node_metadata_from_library("my_library", "Agent")
        """
        request = GetNodeMetadataFromLibraryRequest(library=library_name, node_type=node_type_name)
        result = GriptapeNodes().handle_request(request)
        return result

    # FLOW OPERATIONS
    @classmethod
    def run_flow(cls, flow_name: str) -> ResultPayload:
        """Executes a flow from start to finish.

        Args:
            flow_name (str): Name of the flow to execute.

        Returns:
            ResultPayload: Contains the result of the flow execution.

        Example:
            # Run a flow
            result = cmd.run_flow("my_flow")
        """
        request = StartFlowRequest(flow_name=flow_name, debug_mode=False)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def run_node(cls, node_name: str) -> ResultPayload:
        """Executes a single node.

        Args:
            node_name (str): Name of the node to execute.

        Returns:
            ResultPayload: Contains the result of the node execution.

        Example:
            # Run a single node
            result = cmd.run_node("my_node")
        """
        request = ResolveNodeRequest(node_name=node_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def single_step(cls, flow_name: str) -> ResultPayload:
        """Executes a single step in a flow.

        This method executes one node in the flow and stops, allowing for
        step-by-step debugging or controlled execution.

        Args:
            flow_name (str): Name of the flow to step through.

        Returns:
            ResultPayload: Contains the result of the step execution.

        Example:
            # Execute one step in a flow
            result = cmd.single_step("my_flow")
        """
        request = SingleNodeStepRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def single_execution_step(cls, flow_name: str) -> ResultPayload:
        """Executes a single execution step in a flow.

        Similar to single_step but focuses on execution steps rather than
        individual nodes. This is useful for flows with complex execution patterns.

        Args:
            flow_name (str): Name of the flow to step through.

        Returns:
            ResultPayload: Contains the result of the execution step.

        Example:
            # Execute one execution step in a flow
            result = cmd.single_execution_step("my_flow")
        """
        request = SingleExecutionStepRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def continue_flow(cls, flow_name: str) -> ResultPayload:
        """Continues execution of a paused flow.

        Use this method to resume execution after a flow has been paused
        (e.g., after a single step).

        Args:
            flow_name (str): Name of the flow to continue.

        Returns:
            ResultPayload: Contains the result of continuing the flow.

        Example:
            # Continue a paused flow
            result = cmd.continue_flow("my_flow")
        """
        request = ContinueExecutionStepRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def reset_flow(cls, flow_name: str) -> ResultPayload:
        """Resets a flow to its initial state.

        This clears any execution state and allows the flow to be run again
        from the beginning.

        Args:
            flow_name (str): Name of the flow to reset.

        Returns:
            ResultPayload: Contains the result of resetting the flow.

        Example:
            # Reset a flow to its initial state
            result = cmd.reset_flow("my_flow")
        """
        request = UnresolveFlowRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def cancel_flow(cls, flow_name: str) -> ResultPayload:
        """Cancels the execution of a running flow.

        Args:
            flow_name (str): Name of the flow to cancel.

        Returns:
            ResultPayload: Contains the result of canceling the flow.

        Example:
            # Cancel a running flow
            result = cmd.cancel_flow("my_flow")
        """
        request = CancelFlowRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def get_flow_state(cls, flow_name: str) -> ResultPayload:
        """Gets the current state of a flow.

        This includes information about which nodes have been executed,
        the current execution position, and any errors that have occurred.

        Args:
            flow_name (str): Name of the flow to get state for.

        Returns:
            ResultPayload: Contains the current state of the flow.

        Example:
            # Get the current state of a flow
            state = cmd.get_flow_state("my_flow")
        """
        request = GetFlowStateRequest(flow_name=flow_name)
        return GriptapeNodes().handle_request(request)

    @classmethod
    def get_metadata_for_flow(cls, flow_name: str) -> ResultPayload:
        """Retrieves metadata associated with a flow.

        Flow metadata can include UI position, display name, tags, and other custom properties.

        Args:
            flow_name (str): Name of the flow to get metadata for.

        Returns:
            ResultPayload: Contains the flow's metadata.

        Example:
            # Get flow metadata
            result = cmd.get_metadata_for_flow("my_flow")
        """
        request = GetFlowMetadataRequest(flow_name=flow_name)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def set_metadata_for_flow(cls, flow_name: str, metadata: dict[Any, Any]) -> ResultPayload:
        """Sets metadata for a flow.

        Args:
            flow_name (str): Name of the flow to set metadata for.
            metadata (dict): Dictionary containing the metadata to set.

        Returns:
            ResultPayload: Contains the result of the metadata update operation.

        Example:
            # Set flow position
            metadata = {
                "position": {"x": 100, "y": 200}
            }
            result = cmd.set_metadata_for_flow("my_flow", metadata)
        """
        request = SetFlowMetadataRequest(flow_name=flow_name, metadata=metadata)
        result = GriptapeNodes().handle_request(request)
        return result

    # ARBITRARY PYTHON EXECUTION
    @classmethod
    def run_arbitrary_python(cls, python_str: str) -> ResultPayload:
        """Executes arbitrary Python code in the context of the current flow.

        This method allows for dynamic execution of Python code, which can be
        useful for custom operations or debugging.

        Args:
            python_str (str): Python code to execute.

        Returns:
            ResultPayload: Contains the result of executing the Python code.

        Example:
            # Execute some Python code
            result = cmd.run_arbitrary_python("print('Hello, World!')")
        """
        request = RunArbitraryPythonStringRequest(python_string=python_str)
        result = GriptapeNodes().handle_request(request)
        return result

    # CONFIG MANAGER
    @classmethod
    def get_config_value(cls, category_and_key: str) -> ResultPayload:
        """Gets a configuration value from the system.

        Args:
            category_and_key (str): Configuration key in format "category.key".

        Returns:
            ResultPayload: Contains the configuration value.

        Example:
            # Get a configuration value
            value = cmd.get_config_value("app_events.events_to_echo")
        """
        request = GetConfigValueRequest(category_and_key=category_and_key)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def set_config_value(cls, category_and_key: str, value: Any) -> ResultPayload:
        """Sets a configuration value in the system.

        Args:
            category_and_key (str): Configuration key in format "category.key".
            value (Any): Value to set.

        Returns:
            ResultPayload: Contains the result of setting the configuration value.

        Example:
            # Set a configuration value
            result = cmd.set_config_value("app_events.events_to_echo", ["event1", "event2"])
        """
        request = SetConfigValueRequest(category_and_key=category_and_key, value=value)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_config_category(cls, category: str | None) -> ResultPayload:
        """Gets all configuration values in a category.

        Args:
            category (str, optional): Category to get values for. If None, returns all categories.

        Returns:
            ResultPayload: Contains the configuration values.

        Example:
            # Get all values in a category
            values = cmd.get_config_category("app_events")

            # Get all categories
            categories = cmd.get_config_category(None)
        """
        request = GetConfigCategoryRequest(category=category)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_workflow_run_command(
        cls,
        workflow_name: str | None = None,
        file_path: str | None = None,
    ) -> ResultPayload:
        """Returns a command-line string to run a workflow using the engine's Python.

        Provide workflow_name (from registry), file_path (path to workflow file), or neither to use
        the workflow in the current context.

        Args:
            workflow_name: Name of the workflow in the registry (optional if file_path or current context).
            file_path: Path to the workflow file, relative to workspace or absolute (optional if workflow_name or current context).

        Returns:
            ResultPayload: On success, result has run_command attribute (e.g. "/path/to/python /path/to/workflow.py").

        Example:
            result = cmd.get_workflow_run_command()  # uses current context workflow
            result = cmd.get_workflow_run_command(workflow_name="My Workflow")
            if result.succeeded():
                print(result.run_command)  # /location/to/griptape/python /location/to/workflow.py
        """
        request = GetWorkflowRunCommandRequest(
            workflow_name=workflow_name,
            file_path=file_path,
        )
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def set_config_category(cls, category: str | None, contents: dict[str, Any]) -> ResultPayload:
        """Sets multiple configuration values in a category.

        Args:
            category (str, optional): Category to set values for. If None, sets root-level values.
            contents (dict): Dictionary of key-value pairs to set.

        Returns:
            ResultPayload: Contains the result of setting the configuration values.

        Example:
            # Set multiple values in a category
            values = {
                "key1": "value1",
                "key2": "value2"
            }
            result = cmd.set_config_category("my_category", values)
        """
        request = SetConfigCategoryRequest(category=category, contents=contents)
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def get_config_schema(cls) -> ResultPayload:
        """Gets the JSON schema for the configuration model.

        Returns:
            ResultPayload: Contains the configuration schema with field types, enums, and validation rules.

        Example:
            # Get the configuration schema
            schema_result = cmd.get_config_schema()
            if isinstance(schema_result, GetConfigSchemaResultSuccess):
                schema = schema_result.schema
                # Use schema to render appropriate UI components
        """
        request = GetConfigSchemaRequest()
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def rename(cls, object_name: str, requested_name: str) -> ResultPayload:
        """Renames a node or flow.

        Args:
            object_name (str): Current name of the object to rename.
            requested_name (str): New name to assign.

        Returns:
            ResultPayload: Contains the result of the rename operation.

        Example:
            # Rename a node
            result = cmd.rename("old_node_name", "new_node_name")
        """
        request = RenameObjectRequest(
            object_name=object_name,
            requested_name=requested_name,
            allow_next_closest_name_available=True,
        )
        result = GriptapeNodes().handle_request(request)
        return result

    @classmethod
    def ls(cls, **kwargs) -> list:
        """Lists objects in the system.

        Args:
            **kwargs: Optional filters to apply to the listing.

        Returns:
            list: List of object names matching the filters.

        Example:
            # List all objects
            objects = cmd.ls()

            # List objects with a specific filter
            filtered_objects = cmd.ls(type="node")
        """
        rsl = GriptapeNodes.ObjectManager()
        as_dict = rsl.get_filtered_subset(**kwargs)
        return list(as_dict.keys())

    @classmethod
    def help(cls, command_name: str | None = None) -> str:
        """Returns help text for a specific command or all commands if none is provided.

        Args:
            command_name (str, optional): Name of the command to get help for.

        Returns:
            str: Help text for the specified command or list of all commands.

        Example:
            # Get help for a specific command
            help_text = cmd.help("create_node")

            # List all available commands
            all_commands = cmd.help()
        """
        if command_name:
            func = getattr(cls, command_name, None)
            if not func or not callable(func):
                return f"[red]X[/red] No such command: {command_name}"

            doc = inspect.getdoc(func) or "No documentation available."
            sig_lines = _fancy_signature(func)

            help_text = [f"Help for: {command_name}()", "", *doc.splitlines(), "", *sig_lines]
            return "\n".join(help_text)

        lines = ["📚 Available commands:\n"]
        for name in dir(cls):
            if not name.startswith("_"):
                attr = getattr(cls, name)
                if callable(attr):
                    lines.append(f"- {name}")
        lines.append("\nUse cmd.help('command_name') to get more info.")
        return "\n".join(lines)


def _fancy_signature(func: Callable) -> list[str]:
    """Return a dev-friendly, neatly aligned function signature."""
    import inspect
    from typing import get_type_hints

    sig = inspect.signature(func)
    type_hints = get_type_hints(func)

    def type_repr(tp) -> str:  # noqa: ANN001
        if tp is inspect.Signature.empty:
            return "Any"
        if isinstance(tp, type):
            return tp.__name__
        tp_str = str(tp)
        return tp_str.replace("typing.", "").replace("<class '", "").replace("'>", "").split(".")[-1]

    params = []
    max_name_len = 0
    max_type_len = 0

    for name, param in sig.parameters.items():
        annotation = type_hints.get(name, param.annotation)
        annotation_str = type_repr(annotation)
        is_optional = param.default is not inspect.Parameter.empty
        default = f"= {param.default!r}" if is_optional else ""
        optional_flag = "[optional]" if is_optional else "[required]"

        params.append((name, annotation_str, default, optional_flag))
        max_name_len = max(max_name_len, len(name))
        max_type_len = max(max_type_len, len(annotation_str))

    lines = ["Function definition:"]
    lines.append(f"    {func.__name__}(")

    for name, annotation_str, default, flag in params:
        lines.append(
            f"        {name.ljust(max_name_len)}: {annotation_str.ljust(max_type_len)} {default.ljust(20)} {flag}"
        )

    lines.append("    )")

    lines.append("")
    lines.append("Parameters marked [optional] have default values.")
    return lines
