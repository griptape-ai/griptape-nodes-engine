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


@dataclass
class LocalObjectEntry:
    """A live object held for this process, plus what is needed to release it.

    `on_drop` exists because deleting the entry does not free what the object was holding.
    """

    value: Any
    owner: str
    source: str
    on_drop: Callable[[Any], None] | None = None


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

    def put_local_object(
        self,
        value: Any,
        *,
        owner: str,
        source: str,
        key: str | None = None,
        on_drop: Callable[[Any], None] | None = None,
    ) -> str:
        """Hold `value` in this process and return the key that refers to it.

        The key is namespaced by `owner`, so two owners choosing the same suffix scheme cannot collide.
        Supplying `key` reuses a slot the caller can name again (a config hash); omitting it uses
        `source`.
        """
        # The suffix defaults to the source rather than to something unique per put, so that putting
        # again displaces what the same source put last time and routes it through `on_drop`. A random
        # suffix would leave every earlier object resident and unreachable.
        suffix = key if key is not None else source
        full_key = self.local_object_key(suffix, owner=owner)
        entry = LocalObjectEntry(
            value=value,
            owner=owner,
            source=source,
            on_drop=on_drop,
        )
        with self._local_objects_lock:
            displaced = self._local_objects.get(full_key)
            self._local_objects[full_key] = entry

        # Reusing a key replaces what was there, and that entry's release hook still has to run:
        # dropping the last reference does not free what the object was holding, which is the whole
        # reason on_drop exists. Rebuilding a pipeline under the same config hash would otherwise
        # strand its GPU memory on every rebuild.
        #
        # Identity check, not just presence: re-registering the SAME object under its own key is the
        # obvious way to write the reuse this API recommends, and tearing down the value that is now
        # live in the map would hand the next reader a released object.
        if displaced is not None and displaced.value is not value:
            self._invoke_on_drop(full_key, displaced)
        return full_key

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

        self._invoke_on_drop(key, entry)
        return True

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

    def drain_pending_worker_releases(self) -> list[str]:
        """Take the keys queued for the workers, leaving the queue empty."""
        with self._local_objects_lock:
            pending = self._pending_worker_releases
            self._pending_worker_releases = []
        return pending

    def drop_all_local_objects(self) -> int:
        """Release everything held in this process, returning how many went.

        For clearing workflow state: every key lived in a parameter value, so deleting the nodes makes
        all of them unreachable at once, whoever put them.
        """
        with self._local_objects_lock:
            doomed = dict(self._local_objects)
            self._local_objects.clear()

        for key, entry in doomed.items():
            self._invoke_on_drop(key, entry)
        return len(doomed)

    def drop_objects_for_owner(self, owner: str) -> int:
        """Release everything one owner is holding, returning how many entries went.

        For an owner clearing its own cache, and for library reload, where code that built an object is
        being replaced.
        """
        with self._local_objects_lock:
            doomed = {key: entry for key, entry in self._local_objects.items() if entry.owner == owner}
            for key in doomed:
                del self._local_objects[key]

        for key, entry in doomed.items():
            self._invoke_on_drop(key, entry)
        return len(doomed)

    # Private Implementation Methods

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
