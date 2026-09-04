import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.resource_events import (
    AcquireResourceInstanceLockRequest,
    AcquireResourceInstanceLockResultFailure,
    AcquireResourceInstanceLockResultSuccess,
    CreateResourceInstanceRequest,
    CreateResourceInstanceResultFailure,
    CreateResourceInstanceResultSuccess,
    FreeResourceInstanceRequest,
    FreeResourceInstanceResultFailure,
    FreeResourceInstanceResultSuccess,
    GetExecutionDeviceRequest,
    GetExecutionDeviceResultFailure,
    GetExecutionDeviceResultSuccess,
    GetResourceInstanceStatusRequest,
    GetResourceInstanceStatusResultFailure,
    GetResourceInstanceStatusResultSuccess,
    ListCompatibleResourceInstancesRequest,
    ListCompatibleResourceInstancesResultFailure,
    ListCompatibleResourceInstancesResultSuccess,
    ListRegisteredResourceTypesRequest,
    ListRegisteredResourceTypesResultSuccess,
    ListResourceInstancesByTypeRequest,
    ListResourceInstancesByTypeResultFailure,
    ListResourceInstancesByTypeResultSuccess,
    RegisterResourceTypeRequest,
    RegisterResourceTypeResultSuccess,
    ReleaseResourceInstanceLockRequest,
    ReleaseResourceInstanceLockResultFailure,
    ReleaseResourceInstanceLockResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.resource_components.resource_type import ResourceType
from griptape_nodes.retained_mode.managers.resource_types.compute_resource import ComputeBackend, ComputeInstance

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.resource_components.resource_instance import ResourceInstance

logger = logging.getLogger("griptape_nodes")


@dataclass
class ResourceStatus:
    resource_type: ResourceType
    instance_id: str
    owner_of_lock: str | None
    capabilities: dict[str, Any]

    def is_locked(self) -> bool:
        """Check if this resource is currently locked."""
        return self.owner_of_lock is not None


# Most capable first. A node that wants something else passes it as `preferred`.
DEVICE_PREFERENCE = (ComputeBackend.CUDA, ComputeBackend.MPS, ComputeBackend.CPU)


class ResourceManager:
    """Manager for resource allocation, locking, and lifecycle management."""

    def __init__(self, event_manager: EventManager) -> None:
        self._resource_types: set[ResourceType] = set()
        # Maps instance_id to ResourceInstance objects
        self._instances: dict[str, ResourceInstance] = {}

        # Register event handlers
        event_manager.assign_manager_to_request_type(
            request_type=ListRegisteredResourceTypesRequest, callback=self.on_list_registered_resource_types_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=RegisterResourceTypeRequest, callback=self.on_register_resource_type_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=CreateResourceInstanceRequest, callback=self.on_create_resource_instance_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=FreeResourceInstanceRequest, callback=self.on_free_resource_instance_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=AcquireResourceInstanceLockRequest, callback=self.on_acquire_resource_instance_lock_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=ReleaseResourceInstanceLockRequest, callback=self.on_release_resource_instance_lock_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=GetExecutionDeviceRequest, callback=self.on_get_execution_device_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=ListCompatibleResourceInstancesRequest,
            callback=self.on_list_compatible_resource_instances_request,
        )
        event_manager.assign_manager_to_request_type(
            request_type=GetResourceInstanceStatusRequest, callback=self.on_get_resource_instance_status_request
        )
        event_manager.assign_manager_to_request_type(
            request_type=ListResourceInstancesByTypeRequest, callback=self.on_list_resource_instances_by_type_request
        )

    # Public Event Handlers
    def on_list_registered_resource_types_request(self, _request: ListRegisteredResourceTypesRequest) -> ResultPayload:
        """Handle request to list all registered resource types."""
        type_names = []
        for rt in self._resource_types:
            type_names.append(type(rt).__name__)  # noqa: PERF401

        return ListRegisteredResourceTypesResultSuccess(
            resource_type_names=type_names, result_details="Successfully listed registered resource types"
        )

    def on_register_resource_type_request(self, request: RegisterResourceTypeRequest) -> ResultPayload:
        """Handle request to register a new resource type."""
        self._resource_types.add(request.resource_type)

        return RegisterResourceTypeResultSuccess(
            result_details=f"Successfully registered resource type {type(request.resource_type).__name__}"
        )

    def on_create_resource_instance_request(self, request: CreateResourceInstanceRequest) -> ResultPayload:
        """Handle request to create a new resource instance."""
        resource_type = self._get_resource_type_by_name(request.resource_type_name)
        if not resource_type:
            return CreateResourceInstanceResultFailure(
                result_details=f"Attempted to create resource instance with resource type {request.resource_type_name} and capabilities {request.capabilities}. Failed due to resource type not found."
            )

        try:
            new_instance = resource_type.create_instance(request.capabilities)
        except Exception as e:
            return CreateResourceInstanceResultFailure(
                result_details=f"Attempted to create resource instance with resource type {request.resource_type_name} and capabilities {request.capabilities}. Failed due to resource type creation failed: {e}."
            )

        instance_id = new_instance.get_instance_id()
        self._instances[instance_id] = new_instance

        return CreateResourceInstanceResultSuccess(
            instance_id=instance_id, result_details=f"Successfully created resource instance {instance_id}"
        )

    def on_free_resource_instance_request(self, request: FreeResourceInstanceRequest) -> ResultPayload:
        """Handle request to free a resource instance."""
        instance = self._instances.get(request.instance_id)
        if instance is None:
            return FreeResourceInstanceResultFailure(
                result_details=f"Attempted to free resource instance {request.instance_id} with force_unlock={request.force_unlock}. Failed due to resource instance does not exist."
            )

        # Check if resource can be safely freed before touching locks
        if not instance.can_be_freed():
            return FreeResourceInstanceResultFailure(
                result_details=f"Resource instance {request.instance_id} cannot be freed and therefore cannot be deleted."
            )

        if instance.is_locked():
            if not request.force_unlock:
                owner = instance.get_lock_owner()
                return FreeResourceInstanceResultFailure(
                    result_details=f"Attempted to free resource instance {request.instance_id} with force_unlock={request.force_unlock}. Failed due to resource instance is locked by {owner}."
                )

            owner = instance.get_lock_owner()
            instance.force_unlock()

        try:
            instance.free()
        except Exception as e:
            return FreeResourceInstanceResultFailure(
                result_details=f"Attempted to free resource instance {request.instance_id} with force_unlock={request.force_unlock}. Failed to free: {e}."
            )

        del self._instances[request.instance_id]

        return FreeResourceInstanceResultSuccess(
            result_details=f"Successfully freed resource instance {request.instance_id}"
        )

    def on_acquire_resource_instance_lock_request(self, request: AcquireResourceInstanceLockRequest) -> ResultPayload:
        """Handle request to acquire a resource instance lock."""
        resource_type = self._get_resource_type_by_name(request.resource_type_name)
        if not resource_type:
            return AcquireResourceInstanceLockResultFailure(
                result_details=f"Attempted to acquire resource instance lock for owner {request.owner_id} with resource type {request.resource_type_name} and requirements {request.requirements}. Failed due to resource type not found."
            )

        # Get compatible unlocked instances
        compatible_instances = []
        for instance in self._instances.values():
            if instance.is_locked():
                continue
            if instance.get_resource_type() != resource_type:
                continue
            if request.requirements is None:
                compatible_instances.append(instance)
                continue
            if instance.is_compatible_with(request.requirements):
                compatible_instances.append(instance)

        best_instance = resource_type.select_best_compatible_instance(compatible_instances, request.requirements)
        if not best_instance:
            return AcquireResourceInstanceLockResultFailure(
                result_details=f"Attempted to acquire resource instance lock for owner {request.owner_id} with resource type {request.resource_type_name} and requirements {request.requirements}. Failed due to no compatible resource instances available."
            )

        try:
            best_instance.acquire_lock(request.owner_id)
        except Exception as e:
            return AcquireResourceInstanceLockResultFailure(
                result_details=f"Attempted to acquire resource instance lock for owner {request.owner_id} with resource type {request.resource_type_name} and requirements {request.requirements}. Failed due to lock acquisition failed: {e}."
            )

        instance_id = best_instance.get_instance_id()

        return AcquireResourceInstanceLockResultSuccess(
            instance_id=instance_id,
            result_details=f"Successfully acquired lock on resource instance {instance_id} for {request.owner_id}",
        )

    def on_release_resource_instance_lock_request(self, request: ReleaseResourceInstanceLockRequest) -> ResultPayload:
        """Handle request to release a resource instance lock."""
        instance = self._instances.get(request.instance_id)
        if instance is None:
            return ReleaseResourceInstanceLockResultFailure(
                result_details=f"Attempted to release resource instance lock on {request.instance_id} for owner {request.owner_id}. Failed due to resource instance does not exist."
            )

        try:
            instance.release_lock(request.owner_id)
        except Exception as e:
            return ReleaseResourceInstanceLockResultFailure(
                result_details=f"Attempted to release resource instance lock on {request.instance_id} for owner {request.owner_id}. Failed due to lock release failed: {e}."
            )

        return ReleaseResourceInstanceLockResultSuccess(
            result_details=f"Successfully released lock on resource instance {request.instance_id} from {request.owner_id}"
        )

    def on_get_execution_device_request(self, request: GetExecutionDeviceRequest) -> ResultPayload:
        """Answer which compute device to run on, without importing a framework to find out.

        `preferred` wins when this machine has it -- that is the user or config pin. Otherwise the
        answer is the most capable backend present, cuda before mps before cpu.

        A `preferred` value this machine lacks is deliberately NOT an error. A workflow authored on
        a CUDA box should still open and run on a laptop; what changes is the device, not the file.
        """
        available = self._detected_compute_backends()
        if not available:
            return GetExecutionDeviceResultFailure(
                result_details=(
                    "Attempted to determine the execution device. Failed because no compute "
                    "resource instance is registered, so the machine's backends are unknown."
                )
            )

        if request.preferred and request.preferred in available:
            return GetExecutionDeviceResultSuccess(
                device=request.preferred,
                available=available,
                honored_preference=True,
                result_details=f"Using the preferred device '{request.preferred}'.",
            )

        device = next((backend.value for backend in DEVICE_PREFERENCE if backend.value in available), available[0])
        detail = f"Chose '{device}' from {available}."
        if request.preferred:
            detail += f" The preferred '{request.preferred}' is not available on this machine."
        return GetExecutionDeviceResultSuccess(
            device=device, available=available, honored_preference=False, result_details=detail
        )

    def _detected_compute_backends(self) -> list[str]:
        """The machine's compute backends, read off the registered compute resource."""
        for instance in self._instances.values():
            if not isinstance(instance, ComputeInstance):
                continue
            backends = instance.get_capability_value("compute") or []
            return [str(getattr(backend, "value", backend)) for backend in backends]
        return []

    def on_list_compatible_resource_instances_request(
        self, request: ListCompatibleResourceInstancesRequest
    ) -> ResultPayload:
        """Handle request to list compatible resource instances."""
        resource_type = self._get_resource_type_by_name(request.resource_type_name)
        if not resource_type:
            return ListCompatibleResourceInstancesResultFailure(
                result_details=f"Attempted to list compatible resource instances with resource type {request.resource_type_name}, requirements {request.requirements}, and include_locked={request.include_locked}. Failed due to resource type not found."
            )

        # Get compatible instances (with optional locked instances)
        instance_ids = []
        for instance in self._instances.values():
            if instance.is_locked() and not request.include_locked:
                continue
            if instance.get_resource_type() != resource_type:
                continue
            if request.requirements is None:
                instance_ids.append(instance.get_instance_id())
                continue
            if instance.is_compatible_with(request.requirements):
                instance_ids.append(instance.get_instance_id())

        return ListCompatibleResourceInstancesResultSuccess(
            instance_ids=instance_ids,
            result_details=f"Successfully found {len(instance_ids)} compatible resource instances",
        )

    def on_get_resource_instance_status_request(self, request: GetResourceInstanceStatusRequest) -> ResultPayload:
        """Handle request to get resource instance status."""
        instance = self._instances.get(request.instance_id)
        if instance is None:
            return GetResourceInstanceStatusResultFailure(
                result_details=f"Attempted to get resource instance status for {request.instance_id}. Failed due to resource instance not found."
            )

        status = ResourceStatus(
            resource_type=instance.get_resource_type(),
            instance_id=request.instance_id,
            owner_of_lock=instance.get_lock_owner(),
            capabilities=instance.get_all_capabilities_and_current_values(),
        )

        return GetResourceInstanceStatusResultSuccess(
            status=status,
            result_details=f"Successfully retrieved status for resource instance {request.instance_id}",
        )

    def on_list_resource_instances_by_type_request(self, request: ListResourceInstancesByTypeRequest) -> ResultPayload:
        """Handle request to list resource instances by type."""
        resource_type = self._get_resource_type_by_name(request.resource_type_name)
        if not resource_type:
            return ListResourceInstancesByTypeResultFailure(
                result_details=f"Attempted to list resource instances by type {request.resource_type_name} with include_locked={request.include_locked}. Failed due to resource type not found."
            )

        matching_instances = []
        for instance in self._instances.values():
            if instance.get_resource_type() != resource_type:
                continue
            if not request.include_locked and instance.is_locked():
                continue
            matching_instances.append(instance.get_instance_id())

        return ListResourceInstancesByTypeResultSuccess(
            instance_ids=matching_instances,
            result_details=f"Successfully found {len(matching_instances)} resource instances of specified type",
        )

    # Private Implementation Methods

    def _get_resource_type_by_name(self, name: str) -> ResourceType | None:
        """Get a registered resource type by its class name."""
        for resource_type in self._resource_types:
            if type(resource_type).__name__ == name:
                return resource_type
        return None
