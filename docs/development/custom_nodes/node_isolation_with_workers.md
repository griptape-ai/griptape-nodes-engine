# Node Isolation with Workers

This page is the operational guide for isolating your library's **node
execution** in a dedicated Python subprocess, so your library's pinned
dependencies (`torch`, `transformers`, `diffusers`) cannot collide with
another library's. You declare which dependencies are heavy enough to
need that, and the engine routes execution accordingly; there is no
setting for an artist to pick. For the rule catalog that catches
execution-boundary mistakes, see
[Strict Mode Reference](strict_mode.md).

## Vocabulary

A few terms used throughout this page:

- **Orchestrator** — the main Griptape Nodes Python process. It owns
    the flow graph, connections, parameter registry, config, and
    secrets. The editor talks to the orchestrator directly. **Every
    library's node classes are imported here**, including yours.
- **Worker subprocess** — a separate Python process that runs your
    library's `process` methods. A library with execution dependencies
    gets its own. Workers communicate with the orchestrator over a
    WebSocket connection (the **bus**).
- **`pip_dependencies` and `pip_dependencies_exec`** — the two
    dependency sets in your manifest. The first is what importing your
    node modules and constructing your nodes needs; the second is what
    only `process` needs.
- **Library venv and `.venv-exec`** — `pip_dependencies` installs into
    the library venv (`<library>/.venv`), which the orchestrator uses.
    `pip_dependencies_exec` installs into `<library>/.venv-exec`, which
    is on `sys.path` only in the worker.
- **`process` and `aprocess`** — your node's execution method.
    Implement `process(self) -> ...` as you do today; the framework
    wraps it as `async def aprocess(self) -> None` so it can run on the
    worker's event loop. The strict-mode rules describe behavior "from
    inside aprocess," but in practice that means "from inside the
    `process` method you wrote."

## Do my nodes need an execution venv?

The question is not whether to isolate your library. It is whether a
dependency is needed *only* at `process` time.

**Declare `pip_dependencies_exec` for** heavy ML packages your nodes
import inside `process`: `torch`, `transformers`, `diffusers`,
`accelerate`, `peft`, `controlnet-aux`, custom CUDA wheels. These are
the pins that collide with another library's, and keeping them out of
the orchestrator's import path is the whole point.

**Keep in `pip_dependencies`** everything needed to import your node
modules and construct your nodes: the package your node file imports at
module scope, whatever a `Parameter` default or a trait touches,
anything a value hook calls. The orchestrator installs this set, and the
editor, workflow loading, and your parameter behaviors depend on it.
Keep it light.

A library that declares no `pip_dependencies_exec` is entirely edit-time
and runs in the orchestrator, with no worker and no serialization tax.
Declaring the set is what buys dependency isolation, and it costs the
cross-process boundary described below.

The split is a judgment about your own code, not a performance dial. If
a package is imported at module scope in a node file, it is an edit-time
dependency whether you want it to be or not: the orchestrator cannot
import the module without it.

## How to declare execution dependencies

Both sets live under `metadata.dependencies` in
`griptape-nodes-library.json`:

```json
{
    "name": "My Library",
    "library_schema_version": "0.14.0",
    "metadata": {
        "author": "<Your Name>",
        "description": "<Description>",
        "library_version": "0.1.0",
        "engine_version": "0.85.0",
        "tags": ["AI", "Custom"],
        "dependencies": {
            "pip_dependencies": [
                "pillow==11.0.0"
            ],
            "pip_dependencies_exec": [
                "torch==2.4.1",
                "transformers==4.45.2"
            ],
            "pip_install_flags": [
                "--extra-index-url",
                "https://download.pytorch.org/whl/cu121"
            ]
        }
    },
    "categories": [],
    "nodes": []
}
```

Isolation only delivers specific environment control if you pin specific
wheels. A loose `torch>=2.0` resolves to whatever pip finds, which
drifts between developers' machines and users' machines. Pin
`torch==2.4.1`, not `torch>=2.0`. `pip_install_flags` applies to both
installs and is the escape hatch for index URLs and other arguments your
install legitimately needs.

The schema:
[`Dependencies`](https://github.com/griptape-ai/griptape-nodes/blob/main/src/griptape_nodes/node_library/library_registry.py)
in `library_registry.py`.

The older `worker_mode_compatibility` and `suggested_worker_mode`
declarations still parse, so no manifest in the wild fails to load, but
neither one affects where a library's nodes execute. Remove them when
convenient.

## What the orchestrator still does for your library

This is the fact that makes the rest of the page short, and the one an
author coming from an earlier engine will not believe without being told
outright: **the orchestrator imports your node modules for real.** Your
classes are the classes the editor holds. So all of this runs on the
orchestrator, for an execution-dependency library exactly as for any
other:

- `__init__`, including every `add_parameter` call
- `Parameter` `converters`, `validators`, and `traits`, including a
    `Button`'s click handler
- `before_value_set` / `after_value_set` when a user edits a value
- connection hooks: `after_incoming_connection`,
    `after_outgoing_connection`, the `allow_*` validators, and the
    `*_removed` variants

Only `process` goes to the worker.

## What you give up

Cross-process serialization tax. When your worker-side node calls
back into orchestrator-owned state (flow graph, connections,
parameter registry, config, secrets) **during a node execute**, that
request is forwarded over the WebSocket bus. Each call is a network
round-trip, and the returned view is **stale-by-call** — by the time
the worker reads it, the orchestrator may have moved on.

Two practical implications:

- **Pass data into nodes via parameters; don't fetch flow state
    during execution.** Reading connection or peer-node state from
    inside `process` works, but each read is a round-trip to the
    orchestrator and the answer is stale as soon as it arrives.
- **Requests are the sanctioned boundary**, for reads and writes
    alike. Emit the corresponding request
    (`SetParameterValueRequest`, `AddParameterToNodeRequest`,
    `RemoveParameterFromNodeRequest`, etc.) and the engine handles
    the round-trip correctly.

Requests issued **outside** node execution are not forwarded; the worker
answers them against its own state.

## Passing values that cannot be serialized

A pipeline, a latent tensor or a live driver has no data form, so it cannot travel
between your worker and the orchestrator as a parameter value. Mark the producing
output `serializable=False` and the engine holds the object in the process that
built it, sending only a key; the consuming node declares nothing and reads the
object normally. See
[Passing Values That Cannot Be Serialized](passing_unserializable_values.md) for
the full picture, including release hooks for anything holding GPU memory and the
restrictions on containers and cross-library wires.

## Lifecycle changes you need to know

### Each `ExecuteNodeRequest` loads a fresh copy of your node

Before `process` runs, the worker loads a copy of your node the same
way a saved workflow reopens:

1. It creates the node from its class, so everything `__init__`
    declares is there.
1. It applies the structure the orchestrator's node has beyond that:
    parameters a user added in the editor, parameters your hooks or
    requests added, list rows, and type or mode changes made with
    `AlterParameterDetailsRequest`. These are the changes a saved
    workflow records.
1. It sets the node's values with `initial_setup=True`.
    `before_value_set` runs and `after_value_set` does not, exactly as
    when a workflow reopens. A `set_parameter_value` override runs,
    with `initial_setup=True`.

The copy has the orchestrator's parameters, types and values. It has no
connections and no other nodes around it, and nothing your node's code
sends while it loads reaches the orchestrator. Then `process` runs, and
from then on requests go to the orchestrator as usual. Afterwards the
copy is discarded. **Your node holds no in-memory state between calls**:
the next execute gets a new copy.

The supported patterns for moving values:

- **Inputs** arrive in `self.parameter_values` at the start of each
    execute, from the orchestrator's node. Read them inside `process`;
    do not assume the values from a prior call are still present.
- **Outputs** go in `self.parameter_output_values`. The framework
    ships these back to the orchestrator after `process` returns. Set
    `self.parameter_output_values["my_param"] = value` inside
    `process`. Called from inside `process`,
    `self.set_parameter_value("my_param", value)` reaches the same
    place for any parameter that allows OUTPUT, so either spelling is
    safe. A parameter that does not allow OUTPUT has no port to
    publish on, and a value set on it during a run is scratch that
    stays in the worker.
- **Cross-call state that must persist** belongs in the
    orchestrator. Issue a `SetParameterValueRequest` from inside
    `process` to update an authoritative value; on the next execute
    the copy is loaded with it. Do not rely on
    `self.parameter_values[k] = v` mid-execute as a way to carry state
    forward — that mutation does not propagate. The same goes for
    `self.set_parameter_value` on a parameter with no OUTPUT: it writes
    the worker's own copy, which is discarded when `process` returns.

What does **not** work: setting `self.foo = ...` and expecting it to
survive. The next execute gets a fresh copy.

### The rules that keep a worker identical to running in-process

A node that follows these three rules behaves the same in a worker as
in-process. Each is also what a saved workflow needs to reopen
correctly, and strict mode checks each one (see below).

1. **Ask by request.** `process` reads engine and graph state only
    through `GriptapeNodes.handle_request(...)`, never through a manager
    (`GriptapeNodes.ConfigManager()` and the like). In a worker a
    request is answered by the orchestrator; a manager answers from the
    worker's own copy.
1. **State lives in parameters.** `after_value_set` and the
    connection hooks change only the node's parameters and structure,
    not attributes on `self`. Those hooks don't run for a worker's copy,
    so anything `process` needs must come from parameter values or from
    what loading rebuilds. Keep such state in a parameter (hidden if it
    isn't for the user), or derive it from values where it is needed.
1. **Loading doesn't touch the graph.** While `initial_setup` is set,
    and in `before_value_set`, change the node itself
    (parameters, types, visibility) and leave connections and other
    nodes alone. A load makes connections before values, a paste
    after them, and a worker's copy has none, so asking about them
    gives an answer that depends on how the node was loaded.

### Mutating the parameter list during execute does not propagate

`self.add_parameter(...)` and `self.remove_parameter_element(...)`
called from inside `process` (or `aprocess`) apply only to the
worker's copy. The orchestrator's authoritative node never sees the
change.

To mutate parameters during execution, route through the request
bus:

- `AddParameterToNodeRequest` to add a parameter
- `RemoveParameterFromNodeRequest` to remove one

Issue the request via `GriptapeNodes.handle_request(...)` from
inside `process`. The handler-side path propagates the change back
to the orchestrator. The
[`parameter-mutation-during-aprocess`](strict_mode.md) rule fires
on direct in-execute mutations and tells you which one to use.

**The change lands on the orchestrator's node, not the one that is
running.** That is the same rule as everywhere else here — the
orchestrator holds the authoritative node — but it has a
consequence worth stating outright: you cannot read the parameter
back during the execution that added it.
`self.get_parameter_by_name("new_param")` returns `None` in the
worker even though the request succeeded and the editor is already
showing the parameter. The next execute has it, because each copy is
loaded from the orchestrator's node as it is then.

### Value hooks run on the orchestrator only

`before_value_set` / `after_value_set` fire on the orchestrator when a
user edits a value, so hooks that adjust the parameter list in response
to user input — showing or hiding fields when a dropdown changes,
growing a list — work normally at edit time. The worker's copy gets the
resulting parameters from the orchestrator instead of running the hooks
again.

## Configuration, secrets, and the current project propagate automatically

The orchestrator broadcasts `ReloadConfigRequest` and
`RefreshSecretsRequest` to every registered worker after a
successful config or secret mutation. Each worker re-reads the
shared on-disk files and updates its in-memory view. You do not need
to wire this up; it happens automatically.

The active **project** propagates the same way. A worker boots like
the orchestrator: it re-derives the current project from the same
shared on-disk config, so a freshly spawned worker lands on the
orchestrator's project for free. After startup, switching projects in
the orchestrator pushes the new project to every running worker so
that environment variables, directory macros, and situation/path
macros resolve against the same project in both processes. Two cases
are worth knowing:

- A switch that changes library configuration restarts the worker,
    which then re-derives the project on boot.
- A "shallow" switch (same workspace and library config, where only
    the environment, directories, or situations differ) does **not**
    restart the worker. The orchestrator broadcasts the switch and the
    worker adopts the new project in place. The switch completes only
    after the worker has adopted it, so a node dispatched afterward never
    runs against a stale project.

You do not need to wire any of this up. A worker never persists the
project choice back to the shared config; the orchestrator is the
single source of truth.

A subtle point: operator-set OS environment variables (e.g., a
container-injected `OPENAI_API_KEY`) are preserved across **refresh
broadcasts** and **explicit deletes**. The worker's refresh re-reads
the `.env` file but does not overwrite an operator-set value, and
deleting a secret from the file does not pop a colliding OS env
var.

`set_secret(...)` is the one path that does override an OS-set
value, because it represents the user's stated intent. The engine
logs a `WARNING` so the asymmetry is visible.

## Checklist

- [ ] Heavy packages only `process` needs declared in
    `pip_dependencies_exec`, with everything needed to import your node
    modules left in `pip_dependencies`
- [ ] Both sets pinned to specific versions
- [ ] `pip_install_flags` set if your install needs a custom index
    URL or other arguments
- [ ] `process` inputs and outputs serialize, or the output is marked
    `serializable=False`
- [ ] No `add_parameter` / `remove_parameter_element` from inside
    `process`; use `AddParameterToNodeRequest` /
    `RemoveParameterFromNodeRequest` via
    `GriptapeNodes.handle_request(...)` instead
- [ ] Cross-node / flow state passed in via parameters, not fetched
    from inside `process`
- [ ] Engine state read in `process` through requests, never managers
- [ ] No attributes set on `self` from `after_value_set` or connection
    hooks that `process` relies on
- [ ] Under `initial_setup`, no requests about connections or other
    nodes from `set_parameter_value` or `before_value_set`

## Strict mode is your safety net

Run your library locally with the engine and watch the engine's
console output for `strict-mode` lines during a normal node execute.
Worker output appears in the same terminal you launched the engine
from, prefixed with `Worker-<engine-id>` so you can tell it apart
from orchestrator output. Look for both **WARNING** and **ERROR**
entries.

Four rules. Each warns where the node runs in-process and fails the
node's run in a worker:

| Rule                                                   | Checked                                          | Orchestrator | Worker |
| ------------------------------------------------------ | ------------------------------------------------ | ------------ | ------ |
| [`parameter-mutation-during-aprocess`](strict_mode.md) | While `process` runs                             | WARNING      | ERROR  |
| [`graph-access-while-loading`](strict_mode.md)         | While a node is created or loaded                | WARNING      | ERROR  |
| [`state-read-bypassing-requests`](strict_mode.md)      | While `process` runs                             | WARNING      | ERROR  |
| [`state-outside-parameters`](strict_mode.md)           | When `after_value_set` or a connection hook runs | WARNING      | ERROR  |

If a strict-mode line fires, the rule's remediation message names
exactly which guideline above was violated and how to fix it.
