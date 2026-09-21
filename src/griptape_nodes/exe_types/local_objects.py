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
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.elements.containers import ParameterContainer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from griptape_nodes.exe_types.elements.parameter import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.managers.resource_manager import ResourceManager

class KeyVerdict(Enum):
    """What a string turned out to be, as far as this worker's cache is concerned.

    One question with four answers, rather than the several booleans this used to be. Every call site
    switches on the verdict, so "which predicate belongs here" stops being a thing anyone can get wrong --
    and the answers are exhaustive, so a new call site cannot quietly forget a case.
    """

    NOT_A_KEY = auto()
    HELD = auto()
    RELEASED = auto()
    ELSEWHERE = auto()


@dataclass(frozen=True)
class KeyLookup:
    """A verdict, plus what the caller needs to act on it."""

    verdict: KeyVerdict
    value: Any = None
    # Whether the engine took this on a parameter's behalf, rather than the library naming it through
    # `put`. Only the engine's own entries are the engine's to displace or release.
    slot_bound: bool = False

    @property
    def is_a_key(self) -> bool:
        return self.verdict is not KeyVerdict.NOT_A_KEY


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

    def look_up(self, value: Any) -> KeyLookup:
        """What `value` is, as far as this worker's cache is concerned. The one question about a string.

        Every key this cache holds carries this worker's prefix, so anything without it is either another
        process's key -- decided by shape, without touching the store -- or not a key at all.
        """
        if not isinstance(value, str):
            return KeyLookup(KeyVerdict.NOT_A_KEY)
        if not value.startswith(f"{self.owner}:"):
            if self._manager().has_minted_key_shape(value):
                return KeyLookup(KeyVerdict.ELSEWHERE)
            return KeyLookup(KeyVerdict.NOT_A_KEY)
        entry = self._manager().entry_for(value)
        if entry is None:
            return KeyLookup(KeyVerdict.RELEASED)
        return KeyLookup(KeyVerdict.HELD, value=entry.value, slot_bound=entry.is_slot_bound)

    def parked_keys_within(self, value: Any) -> set[str]:
        """Every cache key reachable inside `value`, `value` itself included.

        What the release scan collects and what the save and metadata guards ask about. A key reaches a
        parameter bare or nested, because a container carries its children's values.
        """
        return collect_leaves(value, lambda leaf: self.look_up(leaf).is_a_key)

    def contains_a_parked_object(self, value: Any) -> bool:
        """Whether a cache key is anywhere in `value`, including nested inside it."""
        return bool(self.parked_keys_within(value))

    def release_parked(self, key: Any) -> bool:
        """Release `key` in every process, but only if the engine parked it for this library.

        What node deletion calls once nothing refers to a key. A library-named key is refused: it is the
        library's to release, and stable by construction, so releasing it would drop that resource in
        every process holding it.
        """
        lookup = self.look_up(key)
        # Checked before it goes anywhere: a key with no local record is broadcast to every worker on the
        # assumption one of them holds it, so an ordinary string -- an API token is the documented example
        # -- would otherwise be put on the wire to libraries that never saw it.
        if not lookup.is_a_key:
            return False
        if lookup.verdict is KeyVerdict.HELD and not lookup.slot_bound:
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
        """The object behind `key`, or None if this worker's cache is not holding it."""
        return self.look_up(key).value

    def require(self, key: str, *, parameter_name: str | None = None, node_name: str | None = None) -> Any:
        """The object behind `key`, raising if this process is not holding it.

        Use this rather than improvising a recovery path around `get`: rebuilding from the producing node's
        internals only works while everything shares one process.

        Pass `parameter_name` when the key came from a parameter, so the failure points at the input to look
        at. The producing node cannot be named, because on a miss its record went with the entry.

        Raises:
            RuntimeError: if the object is not held here, with the reason it is not.
        """
        where_node = node_name if node_name is not None else self._source
        lookup = self.look_up(key)
        if lookup.verdict is KeyVerdict.HELD:
            return lookup.value
        if lookup.verdict is KeyVerdict.RELEASED:
            raise RuntimeError(self._released_message(parameter_name=parameter_name, node_name=where_node))
        if lookup.verdict is KeyVerdict.ELSEWHERE:
            raise RuntimeError(self._elsewhere_message(parameter_name=parameter_name, node_name=where_node))
        raise RuntimeError(self._not_a_key_message(key, parameter_name=parameter_name, node_name=where_node))

    def resolve_if_held(self, value: Any, *, parameter_name: str, node_name: str) -> Any:
        """`value`, with anything in it that names a cached object replaced by the object.

        What a node's parameter read goes through, so a library reads its parameter normally and gets the
        object. Whether to translate is a question about the value, not about the parameter doing the
        reading: only the producer declares, and the key then travels down a connection to consumers that
        declare nothing. Containers are walked, because a container carries its children's values.

        Raises:
            RuntimeError: if something in `value` names an object this process cannot hand over.
        """
        if not self._manager().could_hold_a_key():
            return value

        def resolve(leaf: Any) -> Any:
            lookup = self.look_up(leaf)
            if lookup.verdict is KeyVerdict.HELD:
                return lookup.value
            if lookup.verdict is KeyVerdict.RELEASED:
                raise RuntimeError(self._released_message(parameter_name=parameter_name, node_name=node_name))
            if lookup.verdict is KeyVerdict.ELSEWHERE:
                raise RuntimeError(self._elsewhere_message(parameter_name=parameter_name, node_name=node_name))
            return leaf

        return substitute_leaves(value, resolve)

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

    def _not_a_key_message(self, key: Any, *, parameter_name: str | None, node_name: str) -> str:
        """Nothing the cache recognises arrived: unwired, or wired to something that is not a reference."""
        # `is None` first, then the type, then emptiness: asking whether a tensor is empty raises out of
        # numpy, and a one-element tensor answers falsy.
        if key is None or (isinstance(key, str) and not key):
            cause = "nothing is connected to it"
        elif not isinstance(key, str):
            cause = "the value it received is not a reference to a held object"
        else:
            cause = "the value it received is not a reference to a held object"
        return (
            f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: {cause}. "
            f"Connect a node that produces one."
        )

    def _released_message(self, *, parameter_name: str | None, node_name: str) -> str:
        """This worker minted the key and no longer holds what it named, so re-running the producer helps."""
        if parameter_name is not None:
            remedy = f"Re-run whatever is connected to '{parameter_name}'."
        else:
            remedy = "Re-run the node that produces it."
        return (
            f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: it is no longer "
            f"available, which happens after the workflow is reloaded or the node that made it is re-run. "
            f"{remedy}"
        )

    def _elsewhere_message(self, *, parameter_name: str | None, node_name: str) -> str:
        """Minted by some other process, so this one has nothing to look up.

        Either a worker that is still running and still holding it, or one that has since been replaced --
        a respawned worker gets a fresh id, so a key from the old one lands here too. The remedy has to
        cover both, because this process cannot tell them apart.
        """
        return (
            f"Attempted to read {self._where(parameter_name, node_name)}. Failed due to: it is held in "
            f"another process, and an object cannot leave the process that built it. Read it from a node "
            f"that runs in the same place as the one that made it, or re-run that node if its worker has "
            f"restarted since."
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


# --- walking a parameter value -------------------------------------------------------------------
#
# The cache asks three questions of a value, and every one of them has to walk it: is this already data,
# which cached keys are in here, and turn the keys into objects. A parameter value is whatever a node
# assigned, so all three have to survive a container that refers to itself and one whose substructure is
# shared rather than nested. Hand-rolling that guard per question is what produced five separate defects
# in this feature's history, so the rules live here, once:
#
#   * a container reached twice is visited once. Without this, shared substructure is exponential in its
#     depth rather than linear in its size.
#   * a container that reaches itself terminates. What that *means* differs by question, so each one seeds
#     its own answer for the revisit rather than sharing one.
#   * substitution hands back the value it was given when nothing changed, so a node that reads a list and
#     mutates it in place is mutating the stored list.
#
# Nothing outside the cache uses these. Other walks over the same shape -- variable substitution, artifact
# hydration -- are separate concerns that happen to share a spine, and coupling them here would tie the
# cache to code that has no reason to know about it.

_CONTAINER_TYPES = (list, tuple, dict, set)


def _children(container: Any) -> Any:
    return container.values() if isinstance(container, dict) else container


def is_plain_data(value: Any) -> bool:
    """Whether `value` is already something JSON can carry, so the cache has no reason to take it."""
    return _is_plain_data(value, memo={})


def _is_plain_data(value: Any, *, memo: dict[int, bool]) -> bool:
    if isinstance(value, (str, int, float, bool, type(None))):
        return True
    if not isinstance(value, (list, tuple, dict)):
        return False
    if id(value) in memo:
        return memo[id(value)]
    # A back-reference is not data: `json.dumps` refuses a circular structure outright, so the honest
    # answer is that the cache should take this value rather than let the transport fail on it.
    memo[id(value)] = False
    if isinstance(value, dict):
        # json.dumps coerces int/float/bool/None keys rather than refusing them, so a dict keyed by frame
        # number travels perfectly well.
        keys_ok = all(isinstance(key, (str, int, float, bool)) or key is None for key in value)
    else:
        keys_ok = True
    result = keys_ok and all(_is_plain_data(child, memo=memo) for child in _children(value))
    memo[id(value)] = result
    return result


def collect_leaves(value: Any, keep: Callable[[Any], bool]) -> set[Any]:
    """Every leaf inside `value` that `keep` accepts, `value` itself included."""
    found: set[Any] = set()
    _collect_leaves(value, keep, found=found, seen=set())
    return found


def _collect_leaves(value: Any, keep: Callable[[Any], bool], *, found: set[Any], seen: set[int]) -> None:
    if not isinstance(value, _CONTAINER_TYPES):
        if keep(value):
            found.add(value)
        return
    if id(value) in seen:
        return
    seen.add(id(value))
    for child in _children(value):
        _collect_leaves(child, keep, found=found, seen=seen)


def substitute_leaves(value: Any, transform: Callable[[Any], Any]) -> Any:
    """`value` with every leaf replaced by `transform(leaf)`, or `value` itself if nothing changed.

    Sets are walked but cannot be rebuilt: their members would have to be re-hashed after substitution and
    the objects this exists for are routinely unhashable. A set whose members would change raises rather
    than silently handing back the originals.

    Raises:
        TypeError: if a substitution would have to rebuild a set.
    """
    return _substitute_leaves(value, transform, memo={})


def _substitute_leaves(value: Any, transform: Callable[[Any], Any], *, memo: dict[int, Any]) -> Any:
    if not isinstance(value, _CONTAINER_TYPES):
        return transform(value)
    if id(value) in memo:
        return memo[id(value)]
    # Seeded with the original before descending, so a container that reaches itself terminates and two
    # places referring to one container get one substituted container back rather than two.
    memo[id(value)] = value
    originals = list(_children(value))
    substituted = [_substitute_leaves(child, transform, memo=memo) for child in originals]
    if all(new is old for new, old in zip(substituted, originals, strict=True)):
        return value
    if isinstance(value, set):
        msg = (
            "Attempted to read a value held in this process. Failed due to: it is inside a set, which "
            "cannot be rebuilt around it. Put held values in a list or a dictionary instead."
        )
        raise TypeError(msg)
    if isinstance(value, dict):
        rebuilt: Any = dict(zip(value.keys(), substituted, strict=True))
    elif isinstance(value, tuple):
        rebuilt = tuple(substituted)
    else:
        rebuilt = substituted
    memo[id(value)] = rebuilt
    return rebuilt


def caches_its_values(parameter: Parameter) -> bool:
    """Whether this parameter's values belong in the cache.

    The cache asks; the parameter types do not answer. `serializable=False` is the author's declaration
    that the value cannot be written out, and a container is excluded because it has no single object to
    hold and nowhere to attach a release hook -- its children are ordinary parameters and are cached on
    their own account.
    """
    return not parameter.serializable and not isinstance(parameter, ParameterContainer)


def cache_outputs_for_egress(values: Mapping[str, Any], *, node: BaseNode) -> dict[str, Any]:
    """`values` with anything the cache takes replaced by its key.

    Called where a worker's output values are about to leave the process, and nowhere else, so a node's own
    dicts keep the real objects and a graph that never crosses a boundary caches nothing. Inputs never come
    through here: caching one would mint a key the far side has nothing to resolve against, since the object
    is in the sending process while the node runs elsewhere.

    A declared output goes in the cache unless the value is already plain data -- a key for an API token
    would be unresolvable over there. Everything else passes through exactly as it did before the cache
    existed, including a value the transport can only manage by stringifying.
    """
    cached: dict[str, Any] = {}
    # A copy, because node bodies write their outputs from worker threads and a dict that changes size
    # mid-iteration raises.
    for name, value in dict(values).items():
        parameter = node.get_parameter_by_name(name)
        if parameter is not None and caches_its_values(parameter):
            cached[name] = node.park_for_egress(parameter, value, travels_as_data=is_plain_data(value))
            continue
        cached[name] = value
    return cached
