"""A node's view of the process-local object store.

Values a library passes between nodes are held by their parameter: assigning an object to a
`serializable=False` output holds it and stores a key, and the engine releases that key when it is
replaced or the node goes away. This module is for the other case -- a *resource* the library reuses
across runs, like a pipeline whose load takes 30 seconds -- which needs a key the library can name again.

The cache belongs to the worker, not to a library. One worker may host several libraries and they can
hand objects to each other, because they genuinely share a process; what an object cannot do is leave the
process that built it. The scope binds what a node knows and the store does not: which worker it is in,
which library it came from, and which node produced it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.managers.resource_manager import ResourceManager

_MISSING = object()


class LocalObjectScope:
    """Reads and writes the local object store on behalf of one node.

    The namespace is the worker this node runs in, which is what physically holds the object. Libraries
    sharing that worker share the cache and can pass objects to each other. A key from a different
    process resolves to nothing, because there is nothing here to resolve it to.
    """

    def __init__(self, *, library: str | None, source: str) -> None:
        self._library = library
        self._source = source

    @property
    def owner(self) -> str:
        """The worker these objects live in.

        Every worker is spawned with its own `GTN_ENGINE_ID`, so this is the identity of the process
        holding the object, and a key minted anywhere else is recognisably from somewhere else. Read
        rather than stored: a scope outlives nothing, but the engine reference is fetched lazily anyway.
        """
        return self._manager().engine.engine_identity_manager.engine_id

    @property
    def library(self) -> str | None:
        """Which library this node came from, recorded on what it parks so it can release its own."""
        return self._library

    def put(self, value: Any, *, key: str, on_drop: Callable[[Any], None] | None = None) -> str:
        """Hold `value` for this library under `key`, returning the full key to look it up with.

        `key` is required: for a value flowing between nodes, mark the producing parameter
        `serializable=False` and assign the object to it, which holds it under a key of its own and has the
        engine release it when replaced. This is for a resource the library reuses across runs, where the
        key is something the library can derive again -- a hash of the model and settings.

        Putting again under the same key releases what was there, unless it is the same object, so
        rebuilding under an unchanged hash does not strand the old one. Pass `on_drop` when releasing
        takes more than dropping the reference, which is true of anything holding GPU memory.
        """
        return self._manager().put_local_object(
            value,
            owner=self.owner,
            source=self._source,
            key=self._namespaced(key),
            library=self._library,
            on_drop=on_drop,
        )

    def park(
        self, value: Any, *, parameter_name: str, slot: str | None = None, on_drop: Callable[[Any], None] | None = None
    ) -> str:
        """Hold a value on behalf of a parameter, under a key minted for this assignment.

        For the engine's own use from the write path.

        The key is unique per call so that a stale one is detectably stale. A consumer holding a key from
        the previous run finds it dangling and is told to re-run the producer, which is the honest answer:
        the object it wanted is gone. A key stable across runs would resolve to whatever the producer put
        most recently, and that consumer would read the new object believing it had the old one. Do not
        make the key stable to save the uuid -- the editor has no use for the difference either way, since
        all it can display is the key.

        The slot carries the identity: one object per (owner, source, parameter), and parking into it again
        releases the previous occupant in the process holding it -- which is what frees the last run's
        object, however the parameter values themselves were cleared in between.
        """
        # Straight to the manager: `slot` is what makes an entry the engine's to release and to displace,
        # and it stays off the library-facing `put` on purpose.
        return self._manager().put_local_object(
            value,
            owner=self.owner,
            source=self._source,
            key=f"{self._source}.{parameter_name}#{uuid.uuid4().hex[:8]}",
            slot=slot if slot is not None else parameter_name,
            library=self._library,
            on_drop=on_drop,
        )

    def is_parked_by_engine(self, key: Any) -> bool:
        """Whether `key` names an entry the engine parked, rather than one a library keyed through `put`.

        Recorded on the entry, not inferred from the key's shape: a library key can look like anything,
        including exactly like a minted one (`sd-xl-1.0#a1b2c3d4`).
        """
        return self._manager().is_parked_key(key)

    def parked_keys_within(self, value: Any) -> set[str]:
        """Every parked key reachable inside `value`, `value` itself included.

        One walk answers both questions the engine asks of a stored value: whether it carries a key at all,
        which the save and metadata guards need, and which keys those are, which the release scan needs when
        a node is deleted. They were two walks asking nearly the same thing, and that is exactly how one of
        them came to recurse while the other did not.

        A key reaches a parameter bare or nested, because a container carries its children's values, so
        asking only about the value itself misses one a level down.
        """
        found: set[str] = set()
        self._collect_parked(value, found=found, seen=set())
        return found

    def contains_a_parked_object(self, value: Any) -> bool:
        """Whether a parked key is anywhere in `value`, including nested inside it."""
        return bool(self.parked_keys_within(value))

    def _collect_parked(self, value: Any, *, found: set[str], seen: set[int]) -> None:
        # `seen` does two jobs, and both are load-bearing on the save path: a self-referential value would
        # recurse forever, and one whose substructure is shared rather than cyclic would be visited
        # exponentially. Either takes out the save of a workflow that saved perfectly well before.
        if self.names_a_parked_object(value):
            found.add(value)
            return
        if not isinstance(value, (dict, list, tuple, set)):
            return
        if id(value) in seen:
            return
        seen.add(id(value))
        items = value.values() if isinstance(value, dict) else value
        for item in items:
            self._collect_parked(item, found=found, seen=seen)

    def names_a_parked_object(self, value: Any) -> bool:
        """Whether `value` is an engine-minted key, including one whose object lives in another process.

        For "may this be written out" questions. `is_parked_by_engine` is the exact, local-entry answer.
        """
        return self._manager().names_a_parked_object(value)

    def release_parked(self, key: Any) -> bool:
        """Release `key` in every process, but only if the engine parked it for this library.

        What node deletion calls once nothing refers to a key. A library-named key is refused: it is the
        library's to release, and stable by construction, so releasing it would drop that resource in
        every process holding it.
        """
        # Shape-checked before it goes anywhere: the orchestrator holds no entries at all, so a key with
        # no local record is broadcast to every worker on the assumption one of them parked it. An
        # ordinary string on an unpersistable parameter -- an API token is the documented example -- would
        # otherwise be put on the wire verbatim to libraries that never saw it.
        if not self._manager().names_a_parked_object(key):
            return False
        return self._manager().release_parked_key(key, owner=self.owner)

    def _namespaced(self, suffix: str) -> str:
        """A library-chosen suffix, namespaced within the worker by the library that chose it.

        The worker decides who can resolve a key; the library keeps two co-tenants from colliding. Without
        this, two libraries sharing a worker that both `put` under "config-hash" would silently displace
        each other and hand one the other's object. Sharing on purpose still works -- a library that is
        given the full key can resolve it, because the worker matches.
        """
        return f"{self._library}/{suffix}"

    def key_for(self, suffix: str) -> str:
        """The full key for a suffix this library chose, without putting anything.

        A suffix alone will not find anything, because `put` returns it namespaced. This is how a library
        checks what it already holds before paying to rebuild:

            key = self.local_objects.key_for(config_hash)
            pipe = self.local_objects.get(key)
            if pipe is None:
                pipe = build()
                self.local_objects.put(pipe, key=config_hash, on_drop=release)
        """
        return self._manager().local_object_key(self._namespaced(suffix), owner=self.owner)

    def get(self, key: str) -> Any | None:
        """The object behind `key`, or None if this library is not holding it in this process."""
        if not self._is_own_key(key):
            return None
        return self._manager().get_local_object(key, owner=self.owner)

    def require(self, key: str, *, parameter_name: str | None = None, node_name: str | None = None) -> Any:
        """The object behind `key`, raising if this process is not holding it.

        Use this rather than improvising a recovery path around `get`: rebuilding from the producing
        node's internals only works while everything shares one process.

        Pass `parameter_name` when the key came from a parameter, so the failure points at the input to
        look at. The producing node cannot be named, because on a miss its record went with the entry.

        Raises:
            RuntimeError: if the object is not held.
        """
        where_node = node_name if node_name is not None else self._source
        if not self._is_own_key(key):
            raise RuntimeError(self._unusable_key_message(key, parameter_name=parameter_name, node_name=where_node))

        # One lookup against a sentinel, rather than asking whether it is held and then reading it: a
        # concurrent drop between those two calls would make this return None from a method contracted to
        # raise, and the caller would fail somewhere deeper with no useful message.
        value = self._manager().get_local_object(key, owner=self.owner, default=_MISSING)
        if value is _MISSING:
            raise RuntimeError(self._gone_message(parameter_name=parameter_name, node_name=where_node))
        return value

    def resolve_if_held(self, value: Any, *, parameter_name: str, node_name: str) -> Any:
        """`value`, with anything in it that names a held object replaced by the object.

        What a node's parameter read goes through, so a library reads its parameter normally and gets the
        object. Whether to translate is a question about the value, not about the parameter doing the
        reading: the producer's `serializable=False` is what parked the object, and the key then travels
        down a connection to consumers that declare nothing. Gating on the reader's own declaration hands
        a key string to every consumer that did not also declare the flag. A value naming nothing held is
        returned untouched.

        Raises:
            RuntimeError: if a key names an object this process cannot hand over.
        """
        return self._resolve_within(value, parameter_name=parameter_name, node_name=node_name, memo={})

    def source_of(self, key: Any) -> str | None:
        """Which node parked `key`, or None if nothing here holds it."""
        return self._manager().source_of(key)

    def key_held_in_slot(self, slot: str, value: Any) -> str | None:
        """The key this node already holds `value` under in `slot`, or None."""
        return self._manager().key_held_in_slot(owner=self.owner, source=self._source, slot=slot, value=value)

    def drop(self, key: str) -> bool:
        """Release one object this library is holding. Returns whether it was released."""
        # An unhashable key -- the held object itself, passed in place of its key -- would raise out of the
        # map lookup. Nothing was released either way, which is what False already means.
        if not isinstance(key, str):
            return False
        return self._manager().drop_local_object(key, owner=self.owner)

    def vacate_slot(self, slot: str, *, keeping: str | None = None) -> None:
        """Release whatever this node parked in `slot`, except the entry behind `keeping`.

        For the egress path, when a run ends with the parameter carrying something other than a fresh
        park: an upstream's key passed through, or None. The upstream's own entry cannot be caught here,
        because it sits under the upstream's source.
        """
        self._manager().vacate_slot(owner=self.owner, source=self._source, slot=slot, keeping=keeping)

    def drop_all(self) -> int:
        """Release everything THIS library is holding in this worker, returning how many went.

        What a "clear cache" node calls. Scoped to the library rather than the worker: the cache is shared
        with whatever else lives here, and emptying a co-tenant's objects is not this node's business.
        """
        # No library is its own bucket rather than a no-op, so a node outside any library can still clear
        # what it put. Only reachable from tests and embedders; every registered node has a library.
        return self._manager().drop_objects_for_library(self._library)

    def _resolve_within(self, value: Any, *, parameter_name: str, node_name: str, memo: dict[int, Any]) -> Any:
        if isinstance(value, str):
            if not self._names_a_held_object(value):
                return value
            if not self._is_own_key(value):
                # A key another library parked is refused rather than handed over as a string: it can
                # never resolve here, and saying so beats the node failing on a str it expected an object
                # to be.
                raise RuntimeError(
                    self._unusable_key_message(value, parameter_name=parameter_name, node_name=node_name)
                )
            return self.require(value, parameter_name=parameter_name, node_name=node_name)
        # Lists and dicts are walked because a container parameter carries its children's values, so a
        # ParameterList fed by three producers holds three keys and `get_parameter_list_value` is what the
        # node reads. Sets and tuples are not: resolving into a set would re-hash objects that are
        # routinely unhashable, and into a tuple subclass would lose what it was. The release scan walks
        # those too, and that asymmetry is safe in the direction it runs -- it counts a key as referenced
        # rather than freeing one still in use.
        if not isinstance(value, (list, dict)):
            return value
        if id(value) in memo:
            return memo[id(value)]
        # Seeded with the original before recursing, so a container reaching itself terminates, and so two
        # rows carrying the same list get the same resolved list rather than the second one coming back
        # with its keys intact.
        memo[id(value)] = value
        originals = list(value.values()) if isinstance(value, dict) else list(value)
        resolved = [
            self._resolve_within(item, parameter_name=parameter_name, node_name=node_name, memo=memo)
            for item in originals
        ]
        # Nothing in here was a key, so hand back the object that was stored. Rebuilding unconditionally
        # would make every read of an ordinary list or dict parameter a copy, and a node that mutates what
        # it read in place would silently stop persisting the change.
        if all(new is old for new, old in zip(resolved, originals, strict=True)):
            return value
        if isinstance(value, dict):
            rebuilt: Any = dict(zip(value.keys(), resolved, strict=True))
        else:
            rebuilt = resolved
        memo[id(value)] = rebuilt
        return rebuilt

    def _names_a_held_object(self, value: Any) -> bool:
        """Whether `value` names an object held here or in another process.

        The entry is the authority when it is here, and either kind counts: a key its owner named through
        `put` has no shape to match. The minted shape is the fallback for an object sitting in a worker,
        where there is no entry to consult and handing the node a string is the wrong answer.
        """
        manager = self._manager()
        return manager.holds_any_key(value) or manager.names_a_parked_object(value)

    def _is_own_key(self, key: Any) -> bool:
        """Whether this worker minted `key`, which is the same question as whether it can resolve it."""
        return isinstance(key, str) and key.startswith(f"{self.owner}:")

    def _unusable_key_message(self, key: Any, *, parameter_name: str | None, node_name: str) -> str:
        # A key arrives as a parameter value, so it can be any type or missing entirely, and each way of
        # being wrong calls for a different fix by whoever built the graph. `is None` first, then the type,
        # then emptiness: asking whether a tensor is empty raises out of numpy, and a one-element tensor
        # answers falsy.
        if key is None:
            cause = "nothing is connected to it"
            remedy = "Connect a node that produces one."
        elif not isinstance(key, str):
            cause = "the value it received is not a reference to a held object"
            remedy = "Connect a node that produces one."
        elif not key:
            cause = "nothing is connected to it"
            remedy = "Connect a node that produces one."
        else:
            # Minted somewhere else: another worker, or the orchestrator reading what a worker made. The
            # object may be perfectly alive over there, so this must not send anyone off to re-run a
            # producer that already succeeded.
            cause = "it is held in another process, and an object cannot leave the process that built it"
            remedy = (
                "Read it from a node that runs in the same place as the one that made it, or have that node "
                "output a saved file instead."
            )
        return f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: {cause}. {remedy}"

    def _gone_message(self, *, parameter_name: str | None, node_name: str) -> str:
        """Why a key minted in THIS worker no longer resolves: whatever it named has been released.

        A key from anywhere else never reaches here -- it is not this worker's to look up, and
        `_unusable_key_message` says so instead.
        """
        if parameter_name is not None:
            remedy = f"Re-run whatever is connected to '{parameter_name}'."
        else:
            remedy = "Re-run the node that produces it."
        return (
            f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: it is no longer "
            f"available, which happens after the workflow is reloaded or the node that made it is re-run. "
            f"{remedy}"
        )

    @staticmethod
    def _where(parameter_name: str | None, node_name: str) -> str:
        if parameter_name is not None:
            return f"the value for parameter '{parameter_name}' on node '{node_name}'"
        return f"a value that node '{node_name}' needs"

    @staticmethod
    def _manager() -> ResourceManager:
        # Lazy: exe_types cannot import the retained_mode package at module scope. One place, rather than
        # once per call site, because BaseNode has no engine reference to hand down.
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.ResourceManager()
