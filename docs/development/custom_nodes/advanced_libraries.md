# Advanced Libraries

Most libraries are fully described by their `griptape_nodes_library.json` manifest: the
engine reads it, imports the node modules it names, and registers those classes. An
**advanced library** is an optional Python class that lets your library run code at four
points in its own lifecycle: before its nodes load, after they load, before it is
unregistered, and when the engine collects request handlers.

This page is the author's reference for that class. For the manifest itself (metadata,
categories, declarations, dependency management) see
[Authoring Libraries](authoring_libraries.md).

## Should I write one?

**You don't need one if** your library is a fixed set of node classes in files you can
list in the manifest. That is the common case and it needs no Python beyond the nodes.

**Write one if** you need to:

- **Acquire or release process-wide resources** at library load and unload: a GPU
    context, a Python binding to a native SDK, a background thread, a connection pool.
- **Register node types the manifest doesn't list**, because the set is data-driven or
    generated. See
    [Registering node types without listing them](#registering-node-types-without-listing-them-in-the-manifest).
- **Serve a request type** your library owns, so other libraries and nodes can call into
    it. See [`get_request_handlers`](#get_request_handlers).
- **Register a competing provider** for an engine request type, such as a workflow
    publisher. See [Publishing](../../guides/publishing.md).

## Wiring one up

Point the manifest at a Python file with `advanced_library_path`, relative to the
manifest:

```json
{
  "name": "My Library",
  "advanced_library_path": "advanced_library.py",
  "nodes": []
}
```

Then subclass `AdvancedNodeLibrary` in that file and override only the hooks you need.
Every hook has a default no-op implementation:

```python
from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary


class MyLibrary(AdvancedNodeLibrary):
    def after_library_nodes_loaded(self, library_data, library) -> None:
        print(f"Loaded {len(library.get_registered_nodes())} nodes")
```

Three rules govern how the engine finds and builds your class. All three fail the whole
library if broken, so they are worth knowing:

- **The class must be defined in that file.** The engine scans the module for an
    `AdvancedNodeLibrary` subclass whose `__module__` matches the module it just
    imported. A subclass *imported* into the file is skipped. If you want the
    implementation to live in a package, subclass it in the file the manifest names.
- **The first match wins.** The engine takes the first qualifying subclass it finds in
    module order and stops. Define exactly one to avoid ambiguity.
- **`__init__` must take no required arguments.** The engine instantiates your class with
    no arguments. Derive whatever state you need in `__init__` or in the hooks.

If the module fails to import, contains no qualifying subclass, or cannot be
instantiated, the library is marked `UNUSABLE`, registration fails, and the editor shows
an `AdvancedLibraryLoadFailureProblem` carrying the underlying error.

## The load sequence

Understanding where each hook sits is the difference between a hook that works and one
that silently does nothing. Registering a library runs, in order:

| Step | What the engine does                                                |
| ---- | ------------------------------------------------------------------- |
| 1    | Parses and validates `griptape_nodes_library.json`                  |
| 2    | Adds the library directory and its venv site-packages to `sys.path` |
| 3    | Imports your advanced library module and instantiates your class    |
| 4    | Registers the `Library` in the `LibraryRegistry`                    |
| 5    | Persists any library settings the manifest declares                 |
| 6    | **Calls `before_library_nodes_loaded`**                             |
| 7    | Iterates `library_data.nodes`, registering each node type           |
| 8    | Registers the manifest's widgets                                    |
| 9    | **Calls `after_library_nodes_loaded`**                              |
| 10   | **Calls `get_request_handlers`** and registers what it returns      |
| 11   | Computes library fitness and marks the library `LOADED`             |

Two consequences worth internalizing:

- Your class exists (step 3) before the library is registered (step 4), so
    `__init__` cannot look itself up in the `LibraryRegistry`.
- Step 7 reads `library_data.nodes` *after* step 6 has run, which is what makes dynamic
    registration possible.

Unregistering runs in the reverse spirit:

| Step | What the engine does                                             |
| ---- | ---------------------------------------------------------------- |
| 1    | **Calls `before_library_unregistered`**                          |
| 2    | Removes the library's app event listeners and pre-dispatch hooks |
| 3    | Removes the request handlers it registered                       |
| 4    | Unregisters the library's widgets                                |
| 5    | Removes the library from the `LibraryRegistry`                   |

## The hooks

### `before_library_nodes_loaded`

```python
def before_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None: ...
```

Runs after your library is registered but before any node type is. Use it to set up
prerequisites that node imports depend on, or to add node definitions to
`library_data.nodes`.

`library_data` is the live `LibrarySchema` the `Library` holds, not a copy. Mutating it
here changes what the engine loads in step 7 and what it reports afterwards.

If this raises, the engine records a `BeforeLibraryCallbackProblem` and **continues
loading**. The library ends up `FLAWED` rather than failed, so a broken hook produces a
library whose nodes work but whose setup did not run. Do not rely on it having succeeded.

### `after_library_nodes_loaded`

```python
def after_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None: ...
```

Runs once every node type in the manifest is registered. At this point
`library.get_registered_nodes()` returns the full list, so this is the place for work
that needs to see the finished library.

This is also where you register competing-provider event handlers via
`LibraryManager.on_register_event_handler()`, which is how a library advertises itself as
a workflow publisher.

Errors are handled the same way as the before hook: an `AfterLibraryCallbackProblem` is
recorded and loading continues.

### `before_library_unregistered`

```python
def before_library_unregistered(self, library_data: LibrarySchema, library: Library) -> None: ...
```

Runs before the engine tears anything down, so your listeners and handlers are still
registered while it executes. Use it to release what you acquired at load: native
bindings, GPU contexts, background threads, connection pools.

Errors here are **logged and swallowed**. Unregistration continues regardless, so a
failing teardown cannot wedge the engine. The flip side is that a resource you fail to
release is leaked silently.

### `get_request_handlers`

```python
def get_request_handlers(self) -> list[tuple[type[RequestPayload], Callable]]: ...
```

Returns `(request_type, handler)` pairs the engine registers on your behalf, and
deregisters automatically when your library unloads. Both sync and async handlers work.
This is how a library exposes a service its own nodes, other libraries, and external
clients can all call.

There are three pieces to build. See
[the worked example](#example-a-library-that-serves-a-request-type) for all of them in
context.

**1. Define the payloads.** A request type and at least one result type, each a dataclass
registered with `PayloadRegistry` so it can be resolved by name over the WebSocket and
MCP surfaces:

```python
@dataclass
@PayloadRegistry.register
class ConvertColorspaceRequest(RequestPayload):
    color: tuple[float, float, float]
    source: str
    target: str


@dataclass
@PayloadRegistry.register
class ConvertColorspaceResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    color: tuple[float, float, float]


@dataclass
@PayloadRegistry.register
class ConvertColorspaceResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass
```

Put them in their own module that both your advanced library and your nodes import. The
library directory is on `sys.path` by the time either loads, so a plain
`import colorspace_events` resolves, and both files get the same module object and
therefore the same payload classes. Give that module a distinctive name: every library
directory lands on the same `sys.path`, so `events.py` risks resolving to another
library's file.

**2. Return the pair from the hook.**

```python
def get_request_handlers(self) -> list[tuple[type[RequestPayload], Callable]]:
    return [(ConvertColorspaceRequest, self._handle_convert_colorspace)]
```

Annotate the return with a bare `Callable`. The base class declares
`Callable[[RequestPayload], ResultPayload]`, and a handler annotated with your concrete
request type is not assignable to that, because parameter types are contravariant.
Keeping the handler's own annotation precise is worth more than matching the base
signature exactly.

**3. Dispatch it.** Any caller in the orchestrator process sends the request through the
normal bus and gets your result back:

```python
result = GriptapeNodes.handle_request(ConvertColorspaceRequest(color=(1.0, 0.0, 0.0), source="rgb", target="hsv"))
if result.failed():
    msg = f"Attempted to convert a color in '{self.name}'. Failed because {result.result_details}"
    raise RuntimeError(msg)

success = cast("ConvertColorspaceResultSuccess", result)
```

Two rules for callers:

- **Dispatch from `process`, never from `__init__`.** A node constructor that sends a
    request trips the `reentrant-bus-in-init` strict-mode rule, and it can deadlock
    against handlers that await engine startup. See
    [Strict Mode Reference](strict_mode.md).
- **Always handle failure.** The providing library might not be installed, might have
    failed to load, or might have been unloaded. In those cases the request has no handler
    at all, and the engine returns a generic failure result rather than your library's
    failure type. Check `result.failed()` before narrowing to your success type.

Constraints on the mechanism:

- **Your library must own the request type.** Define the `RequestPayload` subclass in
    your own package.
- **One handler per request type, engine-wide.** Registering a type that already has a
    handler raises, surfaced as a `RequestHandlerRegistrationProblem`. For request types
    where several libraries compete and the caller picks one by name, use
    `LibraryManager.on_register_event_handler()` in `after_library_nodes_loaded` instead.
- **Orchestrator only.** A library running isolated in a worker subprocess registers its
    handlers in that worker, where the orchestrator cannot reach them. Requests fail with
    "No manager found". The engine flags this combination with a
    `RequestHandlersWorkerIncompatibleProblem` at load. See
    [Node Isolation with Workers](node_isolation_with_workers.md).

Other code can discover what a loaded library exposes with
`library.get_registered_request_handler_types()`, then inspect each type with
`dataclasses.fields()` and `typing.get_type_hints()`.

## Registering node types without listing them in the manifest

If your node set is data-driven, generated, or simply large enough that hand-maintaining
the manifest is a chore, you can leave `"nodes": []` in the manifest and synthesize the
definitions in `before_library_nodes_loaded`.

This works because of step 6 and step 7 in [the load sequence](#the-load-sequence): the
hook runs first, `library_data` is the same object the engine reads next, and
`library_data.nodes` is an ordinary list.

```python
class MyLibrary(AdvancedNodeLibrary):
    def before_library_nodes_loaded(self, library_data, library) -> None:
        library_data.nodes.extend(
            NodeDefinition(
                class_name=spec["class_name"],
                file_path="generated_nodes.py",
                metadata=NodeMetadata(
                    category="dynamic",
                    description=spec["description"],
                    display_name=spec["display_name"],
                ),
            )
            for spec in load_specs()
        )
```

Nothing the editor reads comes from the manifest's `nodes` list. `ListNodeTypesInLibrary`
and `GetAllInfoForLibrary` both read the in-memory `Library`, so synthesized node types
appear in the node palette exactly like declared ones. Categories are still read from the
manifest, so declare every category you intend to synthesize into. Nothing validates a
node's category against the declared keys, so a mismatch fails quietly: the node type
registers fine, but the editor has no category to file it under.

Definitions added this way are indistinguishable from hand-written ones, which means they
inherit the loader's behavior: lazy module loading, one memoized import per file even
when many classes share it, stable-namespace aliasing so saved workflows can unpickle
values your classes define, per-node problem reporting, and correct fitness.

### Where the classes come from

The engine resolves a node type by importing the file in `NodeDefinition.file_path` and
calling `getattr(module, class_name)`. A module-level `__getattr__`
([PEP 562](https://peps.python.org/pep-0562/)) satisfies that, so one file can back every
node type in the library without a single `class` statement:

```python
def __getattr__(name: str) -> type[DataNode]:
    spec = find_spec(name)
    if spec is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    node_class = build_node_class(spec)
    globals()[name] = node_class  # cache: next lookup skips __getattr__
    return node_class
```

Cache the built class in module globals. Two lookups of the same node type must return
the same object, because the engine caches the resolved class, `isinstance` checks
compare against it, and pickles reference it.

!!! warning "Set `__module__` explicitly when you build a class"

    `type(name, bases, namespace)` does not give you the current module for free. With
    no `__module__` key in the namespace, class creation reads `__name__` from the
    calling frame's globals. Because `BaseNode` subclasses carry `ABCMeta`, that frame
    is inside the standard library's `abc` module, and your class ends up claiming
    `__module__ == "abc"`.

    Nothing complains at load time. The failure appears later: unpickling a saved
    workflow imports `__module__` and looks up `__qualname__` on it, so any workflow
    carrying a value your class defines will fail to reopen. Pass both explicitly:

    ```python
    return type(
        spec["class_name"],
        (DataNode,),
        {
            "__init__": __init__,
            "process": process,
            "__module__": __name__,
            "__qualname__": spec["class_name"],
        },
    )
    ```

### Why not register classes directly?

`Library.register_new_node_type()` and `Library.register_lazy_node_type()` are public,
and calling them from `after_library_nodes_loaded` also registers working node types.
Prefer synthesizing definitions anyway, for two reasons:

- **Fitness.** The engine decides whether a library loaded successfully from the
    manifest-driven loop in step 7. A library with `"nodes": []` that registers
    everything in the after hook is scored `UNUSABLE` and its registration is reported as
    a failure, even though the node types registered fine.
- **Stable namespaces.** The loader also registers a pending stable-module loader so
    `griptape_nodes.node_libraries.<library>.<file>` resolves when a saved workflow
    reopens. Registering a class directly skips that, and you own the problem.

### Limitations

- **Isolated (worker) libraries are not supported.** When a library runs in a worker
    subprocess, the orchestrator rebuilds stub classes from schemas the worker sends
    back, and it resolves each stub's metadata from the manifest's `nodes` list. Node
    types that exist only in the worker's registry are dropped with a warning. Keep
    dynamically registered libraries in the orchestrator, or list their nodes in the
    manifest.
- **Declaration validation only sees the manifest.** Validation of `model_usage` and
    `model_provider_usage` references runs against the manifest read from disk, so those
    declarations on synthesized nodes are never checked. A bad model reference fails at
    runtime instead of at load.
- **Node name collisions still apply.** Synthesized class names go through the same
    cross-library collision check as declared ones, and generated names collide just as
    easily. Prefix them.

## Examples

Two complete, working libraries. Copy either folder into your workspace's `libraries`
directory, register it through the editor's library settings, and restart the engine.

### Example: a library that serves a request type

A library that owns `ConvertColorspaceRequest`, serves it from its advanced library, and
consumes it from its own node:

- [`griptape_nodes_library.json`](example_request_handler_library/griptape_nodes_library.json):
    manifest declaring one node and the advanced library
- [`colorspace_events.py`](example_request_handler_library/colorspace_events.py): the
    request and result payloads, registered with `PayloadRegistry`
- [`advanced_library.py`](example_request_handler_library/advanced_library.py): returns the
    handler from `get_request_handlers` and implements it
- [`nodes.py`](example_request_handler_library/nodes.py): a node that dispatches the
    request from `process()` and handles failure

Add a **Convert Colorspace** node, set `color` to `[0, 0.5, 1]` with `source` `rgb` and
`target` `hsv`, and run it. Any other library in the same engine can now send
`ConvertColorspaceRequest` and get the same answer.

### Example: a library that registers node types dynamically

A library that registers four node types from a JSON file while declaring none in its
manifest:

- [`griptape_nodes_library.json`](example_dynamic_library/griptape_nodes_library.json):
    manifest with `"nodes": []` and one declared category
- [`node_specs.json`](example_dynamic_library/node_specs.json): the data the node set is
    derived from
- [`advanced_library.py`](example_dynamic_library/advanced_library.py): synthesizes one
    `NodeDefinition` per spec in `before_library_nodes_loaded`
- [`generated_nodes.py`](example_dynamic_library/generated_nodes.py): module
    `__getattr__` that builds each `DataNode` subclass on demand

You should see a **Dynamic** category with four nodes. Add an entry to `node_specs.json`
reusing an existing `operator` value, restart, and a fifth node appears with no change to
the manifest or the Python.
