# Strict Mode Reference

> For when and how to run your library isolated in a worker
> subprocess in the first place, see
> [Node Isolation with Workers](node_isolation_with_workers.md). This
> page is the rule catalog that catches isolation incompatibilities.
>
> Throughout this page, "`aprocess`" refers to the framework's async
> wrapper around the `process` method you implemented on your node.
> When a rule says "during `aprocess`," it means "during the node's
> execute, including from inside the `process` method you wrote."

Strict mode is a runtime contract for what node code is allowed to do
across the orchestrator and worker subprocess. When a node violates the
contract, the framework records a named violation and routes it to the
node's result payload so the author sees a remediation message in the
editor instead of a silent no-op, deadlock, or a stack trace that names
the wrong layer.

Strict mode is always on. There is no config flag, env var, or runtime
toggle. Severity is picked per-rule: correctness rules fail execution;
ergonomics rules emit a warning.

## How it surfaces

Violations attach to the `ResultDetails` on the outgoing
`ResultPayload`. In the editor, the node's output panel shows the rule
id, severity, and remediation. On the worker side, correctness
violations elevate a successful `ExecuteNodeResultSuccess` to an
`ExecuteNodeResultFailure`; ergonomics violations stay non-fatal.

Violations are also logged through the `griptape_nodes.strict_mode`
logger. Set it to `WARNING` or lower to see every violation in the
console.

## Rule catalog

Each rule is either a **correctness** rule (fails on both orchestrator
and worker) or an **ergonomics** rule (warns on orchestrator, escalates
to a failure on the worker unless the rule opts out).

### Correctness rules (fail execution)

#### `reentrant-bus-in-init`

A node issued an event-bus request from inside its `__init__`. The
worker library probe runs `__init__` to extract a schema; re-entering
the bus there deadlocks the worker.

**Remediation**: move the call into `aprocess` (or a lifecycle hook
that runs after construction).

### Ergonomics rules (warnings)

#### `parameter-behaviors-dropped-in-schema`

A `Parameter` attached `converters`, `validators`, or `traits` that
are not captured in the worker schema. The orchestrator stub cannot
re-run those behaviors, so UI-side behavior diverges from worker-side
execution.

**Remediation**: re-run the converter / validator logic inside
`process` so the worker still applies it to the actual value, or
accept the divergence as orchestrator-only UI sugar. Note that
moving the logic into `process` loses the inline editor-side
validation feedback — the user only sees a failure when the node
executes.

#### `parameter-mutation-during-aprocess`

A node called `add_parameter` or `remove_parameter_element` during
`aprocess`. On the worker, these mutations apply to the transient
node instance and do not sync back to the orchestrator.

Hydration-time mutations made from `before_value_set` /
`after_value_set` (the standard dynamic-parameter pattern) do
**not** trip this rule.

**Remediation**: emit an `AddParameterToNodeRequest` or
`RemoveParameterFromNodeRequest` so the mutation propagates to the
authoritative orchestrator-side node.

#### `worker-reach-into-orchestrator`

A node running on a worker issued a request whose authoritative state
lives on the orchestrator (flow graph, connections, parameter
registry, config, secrets). The request is forwarded across the
WebSocket bus; each call is a network round-trip and the returned
view is stale-by-call.

The rule fires for forwarded requests issued anywhere during a
node's execution -- including from `before_value_set` /
`after_value_set` during hydration -- not only from `aprocess`.

**Remediation**: if this is an intentional write (e.g. publishing a
parameter value), ignore the warning. If this is a read of flow /
connection state, consider whether the data could be passed in via
parameters instead of fetched per-call.
