"""A node's view of the process-local object store.

Values a library passes between nodes are held by their parameter: assigning an object to a
`handle[...]` output parks it and stores a key, and the engine releases that key when it is replaced or
the node goes away. This module is for the other case -- a *resource* the library reuses across runs,
like a pipeline whose load takes 30 seconds -- which needs a key the library can name again.

The scope binds the two things a node knows and the store does not: which library owns the object, and
which node produced it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.managers.resource_manager import ResourceManager

# The owner recorded for a node no library registered, so its keys cannot land in a real library's
# namespace. Not a valid library name, deliberately.
UNREGISTERED_OWNER = "<unregistered>"

_MISSING = object()


class LocalObjectScope:
    """Reads and writes the local object store on behalf of one node.

    Everything here is scoped to the node's library. A handle from another library resolves to nothing
    rather than to its object: allowing it would work for as long as both libraries happened to share a
    process, and stop the moment either moved to a worker.
    """

    def __init__(self, *, owner: str, source: str) -> None:
        self._owner = owner
        self._source = source

    @property
    def owner(self) -> str:
        """The library these objects belong to.

        A namespace, not an identity: two libraries that both name the same owner share what is under it.
        """
        return self._owner

    def put(self, value: Any, *, key: str, on_drop: Callable[[Any], None] | None = None) -> str:
        """Hold `value` for this library under `key`, returning the full key to look it up with.

        `key` is required: for a value flowing between nodes, give the producing parameter the type
        `handle[<what it holds>]` and assign the object to it, which parks it under a key of its own and
        has the engine release it when replaced. This is for a resource the library reuses across runs,
        where the key is something the library can derive again -- a hash of the model and settings.

        Putting again under the same key releases what was there, unless it is the same object, so
        rebuilding under an unchanged hash does not strand the old one. Pass `on_drop` when releasing
        takes more than dropping the reference, which is true of anything holding GPU memory.
        """
        return self._manager().put_local_object(value, owner=self._owner, source=self._source, key=key, on_drop=on_drop)

    def park(self, value: Any, *, parameter_name: str, on_drop: Callable[[Any], None] | None = None) -> str:
        """Hold a value on behalf of a parameter, under a key minted for this assignment.

        For the engine's own use from the write path. The key is unique per call, so a parameter's value
        changes every time its node runs and the editor hears about it. The slot carries the identity: one
        object per (owner, source, parameter), and parking into it again releases the previous occupant in
        the process holding it -- which is what frees the last run's object, however the parameter values
        themselves were cleared in between.
        """
        # Straight to the manager: `slot` is what makes an entry the engine's to release and to displace,
        # and it stays off the library-facing `put` on purpose.
        return self._manager().put_local_object(
            value,
            owner=self._owner,
            source=self._source,
            key=f"{self._source}.{parameter_name}#{uuid.uuid4().hex[:8]}",
            slot=parameter_name,
            on_drop=on_drop,
        )

    def is_parked_by_engine(self, key: Any) -> bool:
        """Whether `key` names an entry the engine parked, rather than one a library keyed through `put`.

        Recorded on the entry, not inferred from the key's shape: a library key can look like anything,
        including exactly like a minted one (`sd-xl-1.0#a1b2c3d4`).
        """
        return self._manager().is_parked_key(key)

    def release_parked(self, key: Any) -> bool:
        """Release `key` in every process, but only if the engine parked it for this library.

        What node deletion calls once nothing refers to a key. A library-named key is refused: it is the
        library's to release, and stable by construction, so releasing it would drop that resource in
        every process holding it.
        """
        if not isinstance(key, str):
            return False
        return self._manager().release_parked_key(key, owner=self._owner)

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
        return self._manager().local_object_key(suffix, owner=self._owner)

    def get(self, key: str) -> Any | None:
        """The object behind `key`, or None if this library is not holding it in this process."""
        if not self._is_own_key(key):
            return None
        return self._manager().get_local_object(key, owner=self._owner)

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
        value = self._manager().get_local_object(key, owner=self._owner, default=_MISSING)
        if value is _MISSING:
            raise RuntimeError(self._gone_message(parameter_name=parameter_name, node_name=where_node))
        return value

    def resolve_if_held(self, value: Any, *, parameter_name: str, node_name: str) -> Any:
        """`value` itself, or the object behind it when it is one of this library's keys.

        What a node's parameter read goes through, so a library reads its parameter normally and gets the
        object. A value that is not one of our keys is returned untouched: a non-serializable parameter
        may perfectly well hold something the engine never parked.

        Raises:
            RuntimeError: if the value is one of our keys and this process is no longer holding it.
        """
        if not self._is_own_key(value):
            # A key another library parked is refused rather than handed over as a string: it can never
            # resolve here, and saying so beats the node failing on a str it expected an object to be.
            if self._manager().is_parked_key(value):
                raise RuntimeError(
                    self._unusable_key_message(value, parameter_name=parameter_name, node_name=node_name)
                )
            return value
        return self.require(value, parameter_name=parameter_name, node_name=node_name)

    def drop(self, key: str) -> bool:
        """Release one object this library is holding. Returns whether it was released."""
        # An unhashable key -- the held object itself, passed in place of its key -- would raise out of the
        # map lookup. Nothing was released either way, which is what False already means.
        if not isinstance(key, str):
            return False
        return self._manager().drop_local_object(key, owner=self._owner)

    def vacate_slot(self, parameter_name: str, *, keeping: str | None = None) -> None:
        """Release whatever this node parked for `parameter_name`, except the entry behind `keeping`.

        For the engine's write path, when a run ends with the parameter carrying something other than a
        fresh park: an upstream's key passed through, or None. The upstream's own entry cannot be caught
        here, because it sits under the upstream's source.
        """
        self._manager().vacate_slot(owner=self._owner, source=self._source, slot=parameter_name, keeping=keeping)

    def drop_all(self) -> int:
        """Release everything THIS library is holding in this process, returning how many went.

        What a "clear cache" node calls.
        """
        return self._manager().drop_objects_for_owner(self._owner)

    def _is_own_key(self, key: Any) -> bool:
        return isinstance(key, str) and key.startswith(f"{self._owner}:")

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
            cause = "it was produced by a different node library, and values of this kind cannot be passed between libraries"
            remedy = "Connect a node from the same library, or one that outputs a saved file instead."
        return f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: {cause}. {remedy}"

    def _gone_message(self, *, parameter_name: str | None, node_name: str) -> str:
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


def owner_for_library(library_name: str | None) -> str:
    """The store owner for a library, or the shared unregistered namespace when there is none.

    A node built outside library registration (a test, a sandbox script) still needs somewhere to put
    things, and one namespace for all of them lets them pass handles to each other.
    """
    if not library_name:
        return UNREGISTERED_OWNER
    return str(library_name)
