# Strict Mode Reference

> For when and how to isolate your library's node execution in a worker
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
toggle. Severity is picked per-rule: correctness rules fail execution
anywhere; ergonomics rules warn on the orchestrator and fail the node on
a worker, unless the rule opts out of that escalation.

## How it surfaces

Violations attach to the `ResultDetails` on the outgoing
`ResultPayload`. In the editor, the node's output panel shows the rule
id, severity, and remediation. On the worker side, anything that resolves
to ERROR elevates a successful `ExecuteNodeResultSuccess` to an
`ExecuteNodeResultFailure` — which covers the escalating ergonomics rules
as well as the correctness ones, so every rule in the catalog below
fails a node there.

Violations are also logged through the `griptape_nodes.strict_mode`
logger. Set it to `WARNING` or lower to see every violation in the
console.

## Rule catalog

Each rule is either a **correctness** rule (fails on both orchestrator
and worker) or an **ergonomics** rule (warns on orchestrator, escalates
to a failure on the worker unless the rule opts out).

Rules are checked while a node executes, while a node is created or has
its saved state applied (a workflow load, a paste, or a worker loading
its copy), and when an edit runs a node's `after_value_set` or a
connection hook. A violation outside a node's execution is logged and,
for a load, attached to the result of the request that loaded the node.

A node that triggers none of these rules behaves the same in a worker as
it does in-process. See
[Node Isolation with Workers](node_isolation_with_workers.md#the-rules-that-keep-a-worker-identical-to-running-in-process).

### `parameter-mutation-during-aprocess`

An ergonomics rule. A node called `add_parameter` or
`remove_parameter_element` during `aprocess`. On the worker, these
mutations apply to the worker's copy of the node and do not sync back to
the orchestrator.

Mutations made from `before_value_set` / `after_value_set` (the standard
dynamic-parameter pattern) do **not** trip this rule. Those hooks run on
the orchestrator as values are edited, and the worker's copy receives the
parameters they added.

**Remediation**: emit an `AddParameterToNodeRequest` or
`RemoveParameterFromNodeRequest` so the mutation propagates to the
authoritative orchestrator-side node.

### `graph-access-while-loading`

An ergonomics rule. While a node was having its saved state applied —
in `before_value_set` or `set_parameter_value` with `initial_setup`, or
as its creation set its state — it sent a request that reads or changes
connections, its flow or flow variables, or another node. The graph
around a loading node is incomplete: a workflow load makes connections
before values, a paste after them, and a worker's copy of the node has no
connections or other nodes at all. So the answer depends on how the node
was loaded. `__init__` is not checked: a node is in no flow during
`__init__` however it is created, so the answer there is the same
everywhere.

**Remediation**: while `initial_setup` is set, change the node itself
(its parameters, types and visibility) and leave connections and other
nodes alone; the loader makes the connections. Do the graph work when the
value is edited instead.

### `state-read-bypassing-requests`

An ergonomics rule. While a node ran, it reached for a manager through
`GriptapeNodes` (for example `GriptapeNodes.ConfigManager()`) instead of
asking with a request. In-process the manager's answer is right, so this
only warns. In a worker the manager holds that process's own copy of the
state, so the access is refused.

**Remediation**: use `GriptapeNodes.handle_request(...)` with the
matching request, which gives the same answer in-process and in a worker.

### `state-outside-parameters`

An ergonomics rule. A node's `after_value_set` or connection hook added
or replaced an attribute on the node. `after_value_set` doesn't run when
a node loads, so a reopened workflow never has that attribute, and
neither hook runs for a worker's copy of the node, which has no
connections. Anything that reads the attribute behaves differently there.

Attributes are compared by identity before and after the hook, so a
change made in place (appending to a list the node already holds) is not
seen. Attributes every node has from `BaseNode` are not checked. A node
class that keeps an attribute outside its parameters on purpose, because
nothing that runs the node reads it, can list it in
`attributes_outside_parameters`.

**Remediation**: keep the state in a parameter (hidden if it isn't for
the user), or derive it from the node's values where it is needed.
