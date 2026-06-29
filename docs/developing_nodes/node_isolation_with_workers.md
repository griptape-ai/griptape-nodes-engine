# Node Isolation with Workers

This page is the operational guide for running your library
**isolated**: in a dedicated Python subprocess so your library's
pinned dependencies (`torch`, `transformers`, `diffusers`) cannot
collide with another library's. Artists pick this with the
**Shared / Isolated** dropdown in the editor (see
[Libraries](../libraries.md#shared-vs-isolated)); under the hood,
an isolated library runs on a **worker** subprocess. This page is
the author's side of that mechanism. For the rule catalog that
catches isolation mistakes, see
[Strict Mode Reference](strict_mode.md).

## Vocabulary

A few terms used throughout this page:

- **Orchestrator** — the main Griptape Nodes Python process. It owns
    the flow graph, connections, parameter registry, config, and
    secrets. The editor talks to the orchestrator directly.
- **Worker subprocess** — a separate Python process that runs your
    library's nodes. Each library that opts into worker mode gets its
    own. Workers communicate with the orchestrator over a WebSocket
    connection (the **bus**).
- **`process` and `aprocess`** — your node's execution method.
    Implement `process(self) -> ...` as you do today; the framework
    wraps it as `async def aprocess(self) -> None` so it can run on the
    worker's event loop. The strict-mode rules describe behavior "from
    inside aprocess," but in practice that means "from inside the
    `process` method you wrote."
- **Schema probe** — a one-time pass at library load where the worker
    instantiates each registered node class once to discover its
    parameter layout. This runs your `__init__` before any execute
    request arrives.

## Should I opt in?

**Opt in if** your library pins specific versions of heavy ML
packages (`torch`, `transformers`, `diffusers`, `accelerate`,
`peft`, `controlnet-aux`, custom CUDA wheels) and you want to coexist
with other libraries that pin different versions. Worker mode is the
mechanism for cross-library dependency isolation.

**Stay opted out if** your library only uses lightweight, broadly
compatible packages (the standard library, `pydantic`, `griptape`
itself, common HTTP / YAML / JSON tooling). Running in the
orchestrator process avoids the cross-process serialization tax
described below.

If unsure, the safer default is to opt in — the cost is real but
small, and it future-proofs against a downstream user installing a
heavier library next to yours.

## How to opt in

Worker hosting is described by two declarations on your library's
`metadata.declarations` in `griptape-nodes-library.json`. They live
side by side because they answer two different questions:

- **`worker_mode_compatibility`** — whether the library is *compatible*
    with worker hosting. One field, `compatibility`:
    - `COMPATIBLE`: the library can run in either the orchestrator
        process or a dedicated worker subprocess.
    - `INCOMPATIBLE`: the library only works in the orchestrator
        process and must never be hosted on a worker.
- **`suggested_worker_mode`** — where the library *launches* when
    nothing else overrides. One field, `mode`: `ORCHESTRATOR` or
    `WORKER`. Omit the declaration to take the engine default (today:
    orchestrator). The editor exposes a per-library **Shared /
    Isolated** dropdown (Shared = orchestrator, Isolated = worker)
    that lets users flip a `COMPATIBLE` library between modes; this
    declaration is the author's suggested starting point for that
    dropdown.

Omitting both declarations is equivalent to declaring
`worker_mode_compatibility` with `compatibility=COMPATIBLE` and no
`suggested_worker_mode` — the library is capable of worker mode but
launches in the orchestrator until something asks for the flip.

Declaring `worker_mode_compatibility` with
`compatibility=INCOMPATIBLE` and a `suggested_worker_mode` of `WORKER`
is contradictory; library metadata validation rejects that
combination.

```json
{
    "name": "My Library",
    "library_schema_version": "0.10.0",
    "metadata": {
        "author": "<Your Name>",
        "description": "<Description>",
        "library_version": "0.1.0",
        "engine_version": "0.85.0",
        "tags": ["AI", "Custom"],
        "declarations": [
            {
                "type": "worker_mode_compatibility",
                "compatibility": "COMPATIBLE"
            },
            {
                "type": "suggested_worker_mode",
                "mode": "WORKER"
            }
        ],
        "dependencies": {
            "pip_dependencies": [
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

Worker mode only delivers specific environment control if you pin specific
wheels. A loose `torch>=2.0` resolves to whatever pip finds, which
drifts between developers' machines and users' machines. Pin
`torch==2.4.1`, not `torch>=2.0`. `pip_install_flags` is the escape
hatch for index URLs and other arguments your install legitimately
needs.

The schemas:
[`WorkerModeCompatibility`](https://github.com/griptape-ai/griptape-nodes/blob/main/src/griptape_nodes/node_library/library_declarations.py)
and
[`SuggestedWorkerMode`](https://github.com/griptape-ai/griptape-nodes/blob/main/src/griptape_nodes/node_library/library_declarations.py)
in `library_declarations.py`, and
[`Dependencies`](https://github.com/griptape-ai/griptape-nodes/blob/main/src/griptape_nodes/node_library/library_registry.py#L39)
in `library_registry.py`.

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
    scope) works but is expensive and surfaces as the
    [`worker-reach-into-orchestrator`](strict_mode.md) warning.
- **Intentional writes are sanctioned**, not penalized. Emit the
    corresponding request (`SetParameterValueRequest`,
    `AddParameterToNodeRequest`, `RemoveParameterFromNodeRequest`,
    etc.) and the engine handles the round-trip correctly. The strict-
    mode rule's remediation explicitly flags writes as fine to
    ignore.

Requests issued **outside** node execution (during library load or
bootstrap) are not forwarded — the worker is not connected to the
orchestrator at that point. Bus calls from `__init__` reentrantly
hit the worker's own event loop, which is why `__init__` has its own
strict-mode rule (next section).

## Lifecycle changes you need to know

### `__init__` runs during library load

The worker subprocess instantiates each registered node class once
at startup to extract a parameter schema for the orchestrator. Three
implications:

- **No I/O in `__init__`.** Network calls, auth checks, disk reads,
    database connections all block library load. The schema probe has
    a finite timeout, and a class whose `__init__` raises or times out
    is **silently dropped from the exported library** with no rule
    fired. Move I/O into `process` or a lifecycle hook that runs after
    construction.
- **No event-bus calls in `__init__`.** Reentering the bus during
    the schema probe deadlocks the worker. The
    [`reentrant-bus-in-init`](strict_mode.md) correctness rule fails
    the class on this; because it is a correctness-class violation,
    the class is also dropped from the library schema.
- **Parameters declared in `__init__` are the normal pattern.**
    `self.add_parameter(...)` is fine here — the schema probe is the
    one place a node is "supposed to" define its parameter list.

### Each `ExecuteNodeRequest` constructs a fresh node

The worker materializes a transient node from request metadata, runs
`process`, and discards it. **Your node holds no in-memory state
between calls.**

The supported patterns for moving values:

- **Inputs** arrive in `self.parameter_values` at the start of each
    execute, hydrated from the orchestrator's authoritative copy.
    Read them inside `process`; do not assume the values from a prior
    call are still present.
- **Outputs** go in `self.parameter_output_values`. The framework
    ships these back to the orchestrator after `process` returns. Set
    `self.parameter_output_values["my_param"] = value` inside
    `process`.
- **Cross-call state that must persist** belongs in the
    orchestrator. Issue a `SetParameterValueRequest` from inside
    `process` to update an authoritative value; on the next execute
    the new value will hydrate into `self.parameter_values`. Do not
    rely on `self.parameter_values[k] = v` mid-execute as a way to
    carry state forward — that mutation does not propagate.

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
to the orchestrator. Worker subprocesses pick up the new parameter
list on the next execute. The
[`parameter-mutation-during-aprocess`](strict_mode.md) rule fires
on direct in-execute mutations and tells you which one to use.

**Hydration-time mutations are explicitly sanctioned.** The standard
dynamic-pipeline pattern — `before_value_set` / `after_value_set`
adjusts the parameter list as inputs change — does **not** trip this
rule. The rule only fires once the framework has entered
`aprocess_scope()`, which it opens specifically around `process`
execution, not around the input-hydration pass.

### Parameter `converters`, `validators`, and `traits` do not cross to the orchestrator

When the schema probe exports your library, only the scalar-shaped
fields of each `Parameter` (name, type, default, tooltip, allowed
modes) are serialized for the orchestrator's stub copy of the
class. Custom `converters`, `validators`, and `traits` you attached
to a `Parameter` are **not** carried across — they live in the
worker's process and run only when the worker executes the node.

The orchestrator stub still accepts user input on those parameters
and ships values to the worker, but the orchestrator-side UI cannot
re-run your `converters` / `validators` / `traits` to massage or
reject values before they leave the editor. Authors see this as a
[`parameter-behaviors-dropped-in-schema`](strict_mode.md) warning
at library load.

Two workable patterns:

- **Move the validation or transform into `process`.** The worker
    re-runs it on the actual value. The cost is that the editor
    cannot show the user a validation failure inline; they only see
    it when the node executes.
- **Accept the divergence as orchestrator-only UI sugar.** If the
    converter is purely a display nicety (e.g., title-casing a
    string), losing it on the orchestrator is harmless.

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

## "Is my library isolation-ready?" checklist

- [ ] `worker_mode_compatibility` declared in `metadata.declarations`
    with `compatibility: COMPATIBLE` (or omit the declaration entirely
    -- absence is treated as `COMPATIBLE`), plus `suggested_worker_mode`
    with `mode: WORKER` (or omit `suggested_worker_mode` if you want the
    library to launch in the orchestrator by default and let users opt
    in via the GUI)
- [ ] `__init__` does no I/O and issues no event-bus requests
- [ ] No `add_parameter` / `remove_parameter_element` from inside
    `process`; use `AddParameterToNodeRequest` /
    `RemoveParameterFromNodeRequest` via
    `GriptapeNodes.handle_request(...)` instead
- [ ] Cross-node / flow state passed in via parameters, not fetched
    from inside `process`
- [ ] Custom `converters` / `validators` / `traits` either re-run
    inside `process` or accepted as orchestrator-only UI sugar
- [ ] `pip_dependencies` pinned to specific versions
- [ ] `pip_install_flags` set if your install needs a custom index
    URL or other arguments

## Strict mode is your safety net

Run your library locally with the engine and watch the engine's
console output for `strict-mode` lines during a normal node execute.
Worker output appears in the same terminal you launched the engine
from, prefixed with `Worker-<engine-id>` so you can tell it apart
from orchestrator output. Look for both **WARNING** and **ERROR**
entries.

The four rules and their actual severities:

| Rule                                                      | Orchestrator | Worker  | Notes                                                                                                                                                   |
| --------------------------------------------------------- | ------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`reentrant-bus-in-init`](strict_mode.md)                 | ERROR        | ERROR   | Correctness rule. The class is dropped from the library schema.                                                                                         |
| [`parameter-behaviors-dropped-in-schema`](strict_mode.md) | WARNING      | WARNING | Fires during library load when a `Parameter` carries `converters` / `validators` / `traits` that the worker schema cannot serialize. Does not escalate. |
| [`parameter-mutation-during-aprocess`](strict_mode.md)    | WARNING      | ERROR   | Promotes the node's result to a failure on the worker.                                                                                                  |
| [`worker-reach-into-orchestrator`](strict_mode.md)        | n/a          | WARNING | Fires anywhere during node execution including hydration. Does not escalate; intentional writes are explicitly fine to ignore.                          |

If a strict-mode line fires, the rule's remediation message names
exactly which guideline above was violated and how to fix it. A
worker log free of strict-mode WARNING and ERROR entries (apart from
the explicitly-fine-to-ignore writes flagged by
`worker-reach-into-orchestrator`) is the bar for "isolation-ready."
