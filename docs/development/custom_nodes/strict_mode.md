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
as well as the correctness ones, so the only rule in the catalog below
fails a node there.

Violations are also logged through the `griptape_nodes.strict_mode`
logger. Set it to `WARNING` or lower to see every violation in the
console.

## Rule catalog

Each rule is either a **correctness** rule (fails on both orchestrator
and worker) or an **ergonomics** rule (warns on orchestrator, escalates
to a failure on the worker unless the rule opts out).

Rules are checked while a node executes. There is no load-time check.

### `parameter-mutation-during-aprocess`

An ergonomics rule. A node called `add_parameter` or
`remove_parameter_element` during `aprocess`. On the worker, these
mutations apply to the transient node instance and do not sync back to
the orchestrator.

Hydration-time mutations made from `before_value_set` /
`after_value_set` (the standard dynamic-parameter pattern) do
**not** trip this rule.

**Remediation**: emit an `AddParameterToNodeRequest` or
`RemoveParameterFromNodeRequest` so the mutation propagates to the
authoritative orchestrator-side node.
