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
    inside `process` (or from `before_value_set` /
    `after_value_set`, which run during input hydration on the same
    scope) works, but each read is a round-trip to the orchestrator
    and the answer is stale as soon as it arrives.
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

### Each `ExecuteNodeRequest` constructs a fresh node

The worker materializes a transient node from request metadata, runs
`process`, and discards it. **Your node holds no in-memory state
between calls.** This is the single most surprising thing an author
hits, and it is true on every execute: the worker-side node that ran
the previous execution no longer exists.

The supported patterns for moving values:

- **Inputs** arrive in `self.parameter_values` at the start of each
    execute, hydrated from the orchestrator's authoritative copy.
    Read them inside `process`; do not assume the values from a prior
    call are still present.
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
    the new value will hydrate into `self.parameter_values`. Do not
    rely on `self.parameter_values[k] = v` mid-execute as a way to
    carry state forward — that mutation does not propagate. The same
    goes for `self.set_parameter_value` on a parameter with no
    OUTPUT: it writes the worker's own copy, which is discarded when
    `process` returns.

What does **not** work: setting `self.foo = ...` and expecting it to
survive. The next execute gets a fresh node instance.

### Mutating the parameter list during execute does not propagate

`self.add_parameter(...)` and `self.remove_parameter_element(...)`
called from inside `process` (or `aprocess`) apply only to the
transient worker-side node. The orchestrator's authoritative copy
never sees the change.

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
showing the parameter.

It does not reappear on the next execute either. Each execution
builds a fresh worker-side copy from the node class, so parameter
*structure* never carries forward — only values do.

This is the contract, stated once: **a node's parameter structure
must be a deterministic function of its parameter values.** Create
parameters in `__init__`, or create and remove them from a value
hook based on the values being set — the pattern the diffusers VAE
decoder uses, rebuilding its output parameters from the pipeline
value whenever it is set. Structure written that way needs no
syncing at all: the same derivation runs on the orchestrator's
node at edit time and again on the fresh worker copy as its values
hydrate, so every copy converges on the same shape. Structure that
exists only because something added it once — by request, from
`process`, in a previous session — is history, not derivation, and
history does not carry.

Hydration honors the contract from its side. Values are applied in
passes until the structure settles, so a value for a derived
parameter works even when it arrives before the value that derives
it — including chains, where one derived parameter's hook creates
the next. A value for a parameter
that nothing on the copy derives — most commonly one a user added
in the editor — is left unapplied for that run, with a warning
naming the parameter; it does not fail the execution, and the
authoritative value on the orchestrator is untouched.

### Value hooks run in both processes

`before_value_set` / `after_value_set` fire on the orchestrator when a
user edits a value, so hooks that adjust the parameter list in response
to user input — showing or hiding fields when a dropdown changes,
growing a list — work normally at edit time.

Two things to know about those hooks at *execution* time:

- They also run in the worker, as inputs are applied. The
    orchestrator skips hooks for a value that has not changed, but
    the worker's node is freshly built, so every value looks new and
    every hook runs. Keep them cheap and idempotent.
- Anything they do to the node object there is discarded with the
    temporary node. A parameter-list change made from a hook during
    execution follows the rule above: route it through the request
    bus if it needs to persist, and do not expect to read it back in
    that same execution.

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
    worker adopts the new project in place, ahead of any queued node
    execution, so it never runs against a stale project.

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

## Strict mode is your safety net

Run your library locally with the engine and watch the engine's
console output for `strict-mode` lines during a normal node execute.
Worker output appears in the same terminal you launched the engine
from, prefixed with `Worker-<engine-id>` so you can tell it apart
from orchestrator output. Look for both **WARNING** and **ERROR**
entries.

One rule, checked while a node executes:

| Rule                                                   | Orchestrator | Worker | Notes                                                  |
| ------------------------------------------------------ | ------------ | ------ | ------------------------------------------------------ |
| [`parameter-mutation-during-aprocess`](strict_mode.md) | WARNING      | ERROR  | Promotes the node's result to a failure on the worker. |

If a strict-mode line fires, the rule's remediation message names
exactly which guideline above was violated and how to fix it.
