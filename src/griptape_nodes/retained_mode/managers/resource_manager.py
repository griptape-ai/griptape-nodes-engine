import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import Engine, EngineScoped
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


@dataclass
class LocalObjectEntry:
    """A live object held for this process, plus what is needed to release it.

    `on_drop` exists because deleting the entry does not free what the object was holding.
    """

    value: Any
    owner: str
    source: str
    # Which library parked this. Not part of the namespace -- the worker is -- but a library unloading or
    # clearing its own cache has to find its objects among its co-tenants'.
    library: str | None = None
    # Which slot of `source` the engine parked this for, or None when the owner named the key itself.
    # Provenance lives here rather than in the key's shape, because the engine may only ever release
    # what it parked, and an owner-chosen key can look like anything.
    slot: str | None = None
    on_drop: Callable[[Any], None] | None = None

    @property
    def is_slot_bound(self) -> bool:
        """Whether the engine took this on a parameter's behalf, rather than the library naming it.

        `slot` carries both facts deliberately: which slot to displace, and whose entry this is. A separate
        lifetime field would say the same thing twice and every caller would have to keep the two agreeing.
        Only a slot-bound entry is the engine's to displace or release; a library-named one is stable by
        construction and several sources may hold it.
        """
        return self.slot is not None


_SAME_VALUE_UNSET = object()


class ResourceManager(EngineScoped):
    """What this machine has, and what this process is holding.

    Two separate maps, deliberately:

    - `_capability_instances` describes the machine. `OSManager` populates it at boot with one record
      each for OS, CPU and compute backends, and `ListCompatibleResourceInstances` matches a library's
      declared `resources.required` against it to decide whether that library can execute here.
    - `_local_objects` holds live Python objects a library parked in this process, keyed by an opaque
      string that travels as a parameter value. Nothing else in the engine reads them.

    Keeping them apart is load-bearing. The capability query walks every entry it can see and its
    answer gates library executability, so a library's cached pipeline must never appear in it. A
    third unrelated map does not belong here either; it belongs in its own manager.
    """

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        self._resource_types: set[ResourceType] = set()
        # Maps instance_id to ResourceInstance objects describing this machine's capabilities.
        self._capability_instances: dict[str, ResourceInstance] = {}
        # Maps an engine-minted key to a live object a library parked in THIS process.
        self._local_objects: dict[str, LocalObjectEntry] = {}
        # Latched on the first entry and never cleared: see could_hold_a_key.
        self._has_ever_cached = False
        # Release hooks held back because a node was executing: see drain_deferred_releases.
        self._deferred_releases: dict[str, LocalObjectEntry] = {}
        # Node bodies that yield a callable run on real threads (`async_utils.to_thread`), and parallel
        # resolution runs several node tasks at once, so two nodes can be inside these methods together.
        # Every mutation happens under this lock; release hooks run outside it, since a hook is caller
        # code that may be slow or may call back in.
        #
        # The guarantee is narrower than "thread-safe": the MAP is consistent, the held OBJECT is not
        # protected. A reader is not protected against a concurrent drop, which can run a release hook on
        # an object another node is still using. There is no borrow or lease.
        self._local_objects_lock = threading.Lock()

        # Keys released here that a worker may also be holding. The releases happen on sync paths -- a
        # parameter value being written, a node being deleted -- and telling a worker has to be awaited, so
        # the async chokepoints that already exist drain this instead of each caller inventing a loop.
        self._pending_worker_releases: list[str] = []

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
        self._capability_instances[instance_id] = new_instance

        return CreateResourceInstanceResultSuccess(
            instance_id=instance_id, result_details=f"Successfully created resource instance {instance_id}"
        )

    def on_free_resource_instance_request(self, request: FreeResourceInstanceRequest) -> ResultPayload:
        """Handle request to free a resource instance."""
        instance = self._capability_instances.get(request.instance_id)
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

        del self._capability_instances[request.instance_id]

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
        for instance in self._capability_instances.values():
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
        instance = self._capability_instances.get(request.instance_id)
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
        for instance in self._capability_instances.values():
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
        for instance in self._capability_instances.values():
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
        instance = self._capability_instances.get(request.instance_id)
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
        for instance in self._capability_instances.values():
            if instance.get_resource_type() != resource_type:
                continue
            if not request.include_locked and instance.is_locked():
                continue
            matching_instances.append(instance.get_instance_id())

        return ListResourceInstancesByTypeResultSuccess(
            instance_ids=matching_instances,
            result_details=f"Successfully found {len(matching_instances)} resource instances of specified type",
        )

    # Process-Local Object Cache
    #
    # Plain calls, not request handlers: a request carrying a live object would be forwarded to a
    # worker and stringified by `json.dumps(default=str)` on the way.

    def put_local_object(  # noqa: PLR0913 (each is a distinct fact about the entry; a params object would be one caller's convenience)
        self,
        value: Any,
        *,
        owner: str,
        source: str,
        key: str,
        slot: str | None = None,
        library: str | None = None,
        on_drop: Callable[[Any], None] | None = None,
    ) -> str:
        """Hold `value` in this process and return the key that refers to it.

        The key is namespaced by `owner`, so two owners choosing the same suffix scheme cannot collide.

        `slot` marks an entry the engine parked for one of `source`'s parameters. A slot holds one object:
        parking into it again releases the previous occupant, in this process, which is the process that
        holds it. That displacement lives here rather than on the parameter write, because the engine
        clears parameter values through several paths and none of them can be trusted to still hold the
        old key by the time the new one is written.
        """
        full_key = self.local_object_key(key, owner=owner)
        entry = LocalObjectEntry(
            value=value,
            owner=owner,
            source=source,
            slot=slot,
            library=library,
            on_drop=on_drop,
        )
        with self._local_objects_lock:
            displaced = self._local_objects.get(full_key)
            displaced_slot_entries = {}
            if slot is not None:
                displaced_slot_entries = self._take_slot_entries_locked(
                    owner=owner, source=source, slot=slot, keep_key=full_key, keep_value=value
                )
            self._local_objects[full_key] = entry
            self._has_ever_cached = True
            displaced_value_survives = displaced is not None and self._value_still_held_locked(displaced.value)
            if displaced_slot_entries:
                self._pending_worker_releases.extend(displaced_slot_entries)

        # Reusing a key replaces what was there, and that entry's release hook still has to run:
        # dropping the last reference does not free what the object was holding, which is the whole
        # reason on_drop exists. Rebuilding a pipeline under the same config hash would otherwise
        # strand its GPU memory on every rebuild.
        #
        # Identity check, not just presence: re-registering the SAME object under its own key is the
        # obvious way to write the reuse this API recommends, and tearing down the value that is now
        # live in the map would hand the next reader a released object.
        if displaced is not None and displaced.value is not value and not displaced_value_survives:
            self._invoke_hooks_once_per_object({full_key: displaced})
        self._invoke_hooks_once_per_object(displaced_slot_entries)
        if displaced_slot_entries:
            # Releasing an entry here is what queues its key; this drains the queue. A worker's own slot is
            # not reachable from this process -- keys are minted where the object is parked, so a slot
            # occupied in a worker leaves no entry here to displace -- and it is the worker's own next park
            # that displaces it.
            self.engine.worker_manager.schedule_pending_local_object_releases()
        return full_key

    def parked_keys_for(self, *, owner: str, source: str) -> list[str]:
        """The keys of every entry the engine parked for one source.

        The store is the authority on what a node is holding, not the node's current parameter names: a
        parameter renamed or removed after parking leaves an entry behind that no live name can derive,
        and its release hook still has to run when the node goes.
        """
        with self._local_objects_lock:
            return [
                key
                for key, entry in self._local_objects.items()
                if entry.owner == owner and entry.source == source and entry.is_slot_bound
            ]

    def key_held_in_slot(self, *, owner: str, source: str, slot: str, value: Any) -> str | None:
        """The key this slot already holds `value` under, or None.

        Egress parks the same object every time a node's values are sent, so without this a node whose
        output is read twice would mint a second key for one object. Identity, not equality: two equal
        tensors are still two objects, and re-keying one of them would strand the other's entry.
        """
        with self._local_objects_lock:
            for key, entry in self._local_objects.items():
                if entry.owner == owner and entry.source == source and entry.slot == slot and entry.value is value:
                    return key
        return None

    def could_hold_a_key(self) -> bool:
        """Whether a key could plausibly appear in this process at all.

        Asked before walking a parameter value, because a node read is the hottest path this touches and
        most engines never spawn a worker at all. Both halves are latched rather than current: a key this
        process minted and has since released still deserves "re-run the producer" instead of being handed
        back as a string, and so does one from a worker that has since died.
        """
        return self._has_ever_cached or self.engine.worker_manager.has_ever_had_a_worker()

    def entry_for(self, key: str) -> LocalObjectEntry | None:
        """The entry this process holds under `key`, or None. The cache's only exact lookup."""
        with self._local_objects_lock:
            return self._local_objects.get(key)

    def local_object_key(self, suffix: str, *, owner: str) -> str:
        """The key `put_local_object` would produce for this suffix, without putting anything.

        `key` goes in as a suffix and comes back namespaced, so a caller that supplied `config_hash`
        cannot look it up again with `config_hash`, and the miss is silent.
        """
        return f"{owner}:{suffix}"

    def get_local_object(self, key: str, *, owner: str, default: Any = None) -> Any:
        """The held object, or `default` if this process is not holding it for `owner`.

        `owner` is required, as it is for putting: reading across owners would work only while both
        happened to share a process. Pass a sentinel as `default` to tell "not held" from a held None
        in one lookup, which a concurrent drop cannot invalidate halfway.
        """
        with self._local_objects_lock:
            entry = self._local_objects.get(key)
        if entry is None:
            return default
        if entry.owner != owner:
            return default
        return entry.value

    def drop_local_object(self, key: str, *, owner: str | None = None) -> bool:
        """Release one held object. Returns whether it was released.

        Pass `owner` to refuse a key belonging to someone else: releasing it would run their teardown
        under them and leave their still-valid keys reporting the object as gone.
        """
        with self._local_objects_lock:
            entry = self._local_objects.get(key)
            if entry is None:
                return False
            if owner is not None and entry.owner != owner:
                logger.warning(
                    "'%s' attempted to release an object owned by '%s'. Refused: an owner may only "
                    "release what it put.",
                    owner,
                    entry.owner,
                )
                return False
            del self._local_objects[key]
            survives = self._value_still_held_locked(entry.value)

        if not survives:
            self._invoke_hooks_once_per_object({key: entry})
        return True

    def release_parked_key(self, key: str, *, owner: str) -> bool:
        """Release `key` everywhere, but only if the engine parked it. Returns whether it was released here.

        An entry whose key the owner named itself (`slot` is None) is the owner's to release, several
        sources may name it, and it is stable by construction -- releasing it here would drop that
        resource in every process holding it.

        A key with no entry here still goes to the workers: the object this feature exists for lives in a
        worker, and this process cannot check provenance for an entry it does not hold. The worker-side
        handler drops parked entries only, so the refusal above still holds over there.
        """
        with self._local_objects_lock:
            entry = self._local_objects.get(key)
            if entry is not None and (entry.owner != owner or not entry.is_slot_bound):
                return False
            if entry is None:
                self._pending_worker_releases.append(key)
        if entry is None:
            self.engine.worker_manager.schedule_pending_local_object_releases()
            return False
        return self.release_key_everywhere(key, owner=owner)

    def drop_parked_local_object(self, key: str) -> bool:
        """Release one entry, only if the engine parked it. Returns whether it was released.

        The worker half of `release_parked_key`: the orchestrator broadcasts keys it holds no entry for,
        so the provenance check happens here, in the process that has the entry.
        """
        with self._local_objects_lock:
            entry = self._local_objects.get(key)
            if entry is None or not entry.is_slot_bound:
                return False
            del self._local_objects[key]
            survives = self._value_still_held_locked(entry.value)
        if not survives:
            # Immediately, not deferred like a release this process decided on its own. The orchestrator
            # replaced or deleted this value before sending the message, so a node running here now was
            # handed the new one and cannot be holding this object. Waiting would pin the memory for the
            # length of a render.
            self._invoke_hooks_once_per_object({key: entry}, defer_during_execution=False)
        return True

    def vacate_slot(self, *, owner: str, source: str, slot: str, keeping: str | None = None) -> None:
        """Release everything parked in a slot, except the entry behind `keeping`.

        For a run that ends without a fresh park -- the parameter carries an upstream's key, or None --
        where a plain displacement never fires because nothing was put.
        """
        with self._local_objects_lock:
            vacated = self._take_slot_entries_locked(owner=owner, source=source, slot=slot, keep_key=keeping)
            if vacated:
                self._pending_worker_releases.extend(vacated)
        self._invoke_hooks_once_per_object(vacated)
        if vacated:
            self.engine.worker_manager.schedule_pending_local_object_releases()

    def _take_slot_entries_locked(
        self, *, owner: str, source: str, slot: str, keep_key: str | None, keep_value: Any = _SAME_VALUE_UNSET
    ) -> dict[str, LocalObjectEntry]:
        """Remove and return a slot's displaced entries. Caller holds the lock and runs the hooks.

        An entry holding the very object being re-parked is removed but NOT returned: its object is the
        live value, so running its hook or broadcasting its key would tear down what the new key now
        refers to. Re-assigning the same object to an output mid-run is how progress publishing works.
        """
        taken: dict[str, LocalObjectEntry] = {}
        for existing_key, existing in list(self._local_objects.items()):
            if (
                existing_key == keep_key
                or existing.slot != slot
                or existing.owner != owner
                or existing.source != source
            ):
                continue
            del self._local_objects[existing_key]
            if keep_value is not _SAME_VALUE_UNSET and existing.value is keep_value:
                continue
            # One object can sit in several entries -- parked on two outputs, or parked and also cached
            # under a library key. Displacing one entry must not tear the object down while another still
            # hands it out, so an entry whose object survives elsewhere is removed silently: no hook, no
            # broadcast.
            if any(remaining.value is existing.value for remaining in self._local_objects.values()):
                continue
            taken[existing_key] = existing
        return taken

    def release_key_everywhere(self, key: str, *, owner: str) -> bool:
        """Release `key` here and remember to tell the workers, returning whether this process held it.

        The object may be in this process, in a worker, or in both when two libraries share a namespace, so
        the local release cannot tell you whether anything is left holding it. Callers on sync paths use
        this and let `drain_pending_worker_releases` do the rest.
        """
        dropped = self.drop_local_object(key, owner=owner)
        with self._local_objects_lock:
            self._pending_worker_releases.append(key)
        self.engine.worker_manager.schedule_pending_local_object_releases()
        return dropped

    def requeue_pending_worker_releases(self, keys: list[str]) -> None:
        """Put drained keys back, for a send that failed after taking them."""
        with self._local_objects_lock:
            self._pending_worker_releases[:0] = keys

    def drain_pending_worker_releases(self) -> list[str]:
        """Take the keys queued for the workers, leaving the queue empty."""
        with self._local_objects_lock:
            pending = self._pending_worker_releases
            self._pending_worker_releases = []
        return pending

    def drop_all_local_objects(self) -> int:
        """Release everything held in this process, returning how many went.

        For clearing workflow state. A parked entry is unreachable once its nodes are gone, so taking it is
        forced. A library-named entry is not -- its key is a hash the library re-derives, so it would still
        be findable -- and it goes anyway: objects are not kept across workflows, and a cache surviving
        into a different graph would hand out something built for the previous one. The cost is a rebuild
        on the next run, which is the intended trade.
        """
        with self._local_objects_lock:
            doomed = dict(self._local_objects)
            self._local_objects.clear()

        self._invoke_hooks_once_per_object(doomed)
        return len(doomed)

    def drop_objects_for_library(self, library: str | None) -> int:
        """Release everything one library parked in this process, leaving its co-tenants' objects alone.

        The namespace is the worker, so a library sharing a worker with others cannot be found by owner.
        What a library unloading needs, and what a "clear cache" node should do rather than emptying the
        whole worker.
        """
        with self._local_objects_lock:
            doomed = {key: entry for key, entry in self._local_objects.items() if entry.library == library}
            for key in doomed:
                del self._local_objects[key]
            # A co-tenant may hold the same object: they share this worker's cache deliberately, so a
            # library unloading must not tear down what another one is still handing out.
            to_release = {key: entry for key, entry in doomed.items() if not self._value_still_held_locked(entry.value)}

        self._invoke_hooks_once_per_object(to_release)
        return len(doomed)

    def _value_still_held_locked(self, value: Any) -> bool:
        """`_value_still_held` for callers already holding the lock.

        Deciding whether to run a hook has to happen under the same lock hold as the entry's removal:
        two concurrent drops of two entries holding one object would otherwise each see the other's
        entry already gone and both run the hook.
        """
        return any(entry.value is value for entry in self._local_objects.values())

    def drain_deferred_releases(self) -> int:
        """Run the release hooks held back during node execution. Returns how many objects went.

        Called once a node has finished, which is the only point at which it is safe: a hook frees what the
        object holds -- GPU memory, a file handle -- and a consumer that read the object is using it for as
        long as it runs. The map's lock cannot help there, because the consumer stopped consulting the map
        the moment it had the object in hand.
        """
        with self._local_objects_lock:
            deferred = self._deferred_releases
            self._deferred_releases = {}
        if deferred:
            self._run_hooks(deferred)
        return len(deferred)

    def _invoke_hooks_once_per_object(
        self, removed: dict[str, LocalObjectEntry], *, defer_during_execution: bool = True
    ) -> None:
        """Run release hooks for a batch of removed entries, once per distinct object.

        One object can sit in several entries -- parked on two outputs, or parked and also cached under
        a library key -- and a batch removal takes them all at once. Freeing is per object, not per
        entry, so the hook runs once: the first entry carrying one, since an entry may have none.
        """
        if removed and defer_during_execution and self.engine.event_manager.in_node_execution():
            # A node is running and may be using one of these. Held until it finishes rather than freed
            # underneath it; `drain_deferred_releases` runs them then.
            with self._local_objects_lock:
                self._deferred_releases.update(removed)
            return

        self._run_hooks(removed)

    def _run_hooks(self, removed: dict[str, LocalObjectEntry]) -> None:
        seen: set[int] = set()
        for key, entry in removed.items():
            if id(entry.value) in seen:
                continue
            if entry.on_drop is not None:
                seen.add(id(entry.value))
                self._invoke_on_drop(key, entry)

    def _invoke_on_drop(self, key: str, entry: LocalObjectEntry) -> None:
        """Run a dropped entry's release hook, if it has one.

        The caller has already removed the entry, so a teardown that raises cannot leave the entry
        present with its object resident and no later drop to retry it. The hook is arbitrary caller
        code, and its failure must not propagate into whatever triggered the drop -- often a reload
        clearing many entries at once.
        """
        if entry.on_drop is None:
            return
        try:
            entry.on_drop(entry.value)
        except Exception:
            logger.exception(
                "Attempted to release the held object '%s' produced by '%s'. Its cleanup failed, so "
                "whatever it was holding (GPU memory, for example) may not have been freed.",
                key,
                entry.source,
            )

    def _get_resource_type_by_name(self, name: str) -> ResourceType | None:
        """Get a registered resource type by its class name."""
        for resource_type in self._resource_types:
            if type(resource_type).__name__ == name:
                return resource_type
        return None
