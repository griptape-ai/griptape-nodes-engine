# Node Execution Contract: Stateless Venues

Status: proposal for team review. No code accompanies this document.

## 1. Motivation

Today `process()` mutates node state and reaches into engine internals mid-execution. That
means running a node off the orchestrator requires standing up a transient stub node and
policing its divergence from the authoritative node at runtime, which is what strict mode
exists to do.

This design removes the divergence rather than policing it. The execution venue holds **no
authoritative state**: it has no managers of its own, and every state access a node makes
during execution forwards to the orchestrator, which remains the single source of truth.
Divergence stops being a bug class because there is no second copy to diverge.

The important consequence is that node authors keep the API they have. `self` still works,
events still work, streaming still works, and the ~242 `process`/`aprocess` bodies in the
standard library do not need rewriting. What changes is that a manager call becomes
structurally impossible in the venue, so it is a forwarded request instead of a local call.

### Decisions (locked)

| Topic                | Decision                                                                                                                                                                                                                                        |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| State model          | The venue is a stateless execution runtime: no managers, no authoritative state. Every state access forwards to the orchestrator.                                                                                                               |
| Forwarded surface    | Forward everything. The venue proxies the full retained-mode surface rather than an enumerated subset.                                                                                                                                          |
| Purity               | Not a goal. An earlier framing made `process()` a pure function (values in, values out); statelessness achieves correctness by construction without it, so the purity contract is dropped.                                                      |
| Venue location       | Same machine only. Venues are spawned by the orchestrator and co-located, so they share the filesystem. Distributed/remote engines are out of scope for this design.                                                                            |
| Foreign graph access | Allowed. Reads and mutations of other objects are permitted because forwarded state is always authoritative.                                                                                                                                    |
| Opt-in               | Derived from the library definition mechanism: a library defined the new way (manifest class + node decorators, entry-point loaded) is on this contract. Legacy JSON libraries keep today's behavior. No separate per-node or per-library flag. |
| Strict mode          | Delete the framework.                                                                                                                                                                                                                           |
| Reentrancy           | Designed explicitly as a first-class path, not inherited.                                                                                                                                                                                       |
| License enforcement  | Comes for free via the orchestrator's request pre-dispatch hook, and gets stronger than today.                                                                                                                                                  |

## 2. Current state

Anchors are against the engine tree at time of writing.

- **Per-node worker RPC**: `NodeManager.on_execute_node_request` (node_manager.py:3102)
    routes worker-owned libraries to `_execute_node_via_worker` (:3226). The worker
    materializes a fresh transient node per request from serialized metadata
    (`_materialize_transient_node_from_metadata`, :3174) and is stateless between requests
    by construction (:3111): nothing a node sets on itself survives the call.
- **Partial forwarding already exists**: `FORWARDED_REQUEST_TYPES`
    (app/worker_routing.py:105) enumerates the request classes whose handlers are swapped for
    `RemoteHandler` (:239) by `register_remote_handlers` (:287). Forwarding is only live while
    the worker is inside `EventManager.worker_node_execution_scope` (event_manager.py:861),
    wired by `configure_worker_forwarding` (:832) and executed through
    `forward_to_orchestrator` (:925).
- **Strict mode**: `common/strict_mode.py` (336 lines) plus `common/strict_mode_checks.py`
    (198 lines) define the reporter, the scopes, the severity model, and six rules describing
    what stub-based worker execution loses or breaks.
- **Direct manager access in-process**: an orchestrator-hosted library's node calls managers
    directly. A direct manager call is not a dispatched request, so it bypasses request-level
    policy entirely (see section 6).
- **Variables are already pre-resolved for off-orchestrator execution**:
    `aprocess_scope(precomputed_variables)` (node_types.py:135) seeds the `VariableResolver`
    cache from an orchestrator-supplied dict specifically so an executing node needs no
    registry access to resolve `{VAR}` substitutions.
- **Dispatch unit**: per the packaging design, the unit is the subflow, not the node.
    `SubflowNodeGroup.execution_environment` dispatch lives at common/node_executor.py:250-264.

## 3. The model

### 3.1 What "stateless" means

The venue holds no *authoritative, orchestrator-owned* state: no ConfigManager, no
SecretsManager, no ObjectManager, no FlowManager, no LibraryRegistry authority. It is not
stateless in the trivial sense. It legitimately holds:

- Python module state for the libraries it has imported.
- The node objects for the in-flight subflow, which are real instances, not stubs, but are
    non-authoritative working copies.
- Heavy resident resources such as a diffusion pipeline in VRAM. This is the entire reason
    long-lived venues exist, and it survives across dispatches per the venue lifetime policy.

The rule is about *authority*, not about memory: the venue may hold whatever it needs to
compute, and may not be the source of truth for anything the orchestrator owns.

### 3.2 What forwards, and what does not

Everything a node does that touches orchestrator-owned state forwards: config reads, secret
reads, static file operations, graph queries, graph mutations, object lookups, progress and
log emission, parameter output publication, and status updates. The venue implements no
handler for these locally; it proxies them.

The proxy is generic. It deserializes a request, ships it to the orchestrator, awaits the
result, and returns it. It never needs to know what any particular request *means*, which is
why forwarding the entire surface does not require the venue to be a full engine.

**One thing necessarily does not forward: parameter values on edges internal to the
dispatched subflow.** The venue runs its own resolution machine over the subflow, so when
node A produces a value consumed by node B in the same venue, that value is set and read
locally. This is not a performance optimization. It is forced by the same fact that made the
subflow the dispatch unit: a `LatentArtifact` wraps a live tensor and is deliberately
unserializable, so a forwarded read would require the orchestrator to hold something that
cannot cross a process boundary. Values crossing the subflow boundary do forward, and are
serializable by definition because they crossed.

Boundary inputs hydrated at dispatch may still be re-read through a forwarded request. That
is a latency cost rather than a correctness problem, and it is accepted (see risks).

### 3.3 What node authors must change

Almost nothing, which is the point. The contract is:

- You may use `self`. Your node object is yours.
- You may emit events and issue requests. They are serviced by the orchestrator.
- You may not hold a local manager reference and expect it to be authoritative, because the
    venue has none to give you.

Mutating your own parameter set during execution now works correctly rather than silently
failing to sync, because the mutation is a request against the authoritative graph. The
`parameter-mutation-during-aprocess` hazard disappears as a consequence of the architecture.

### 3.4 Why purity is not the mechanism

An earlier framing of this work made `process()` a pure function: parameter values in, output
values out, opt-in per node. That would have delivered the same goal, and it additionally
buys engine-free unit testing, result caching, and parallel safety. It was dropped because
statelessness delivers the stated goal (correct by construction, no divergence to police)
without a migration tax across 242 process bodies, 24 files using the `AsyncResult` yield
pattern, and 18 files streaming mid-execution.

The tradeoff is explicit: `process()` retains unbounded reach, so there is no
unit-testable-without-an-engine story and no memoization story. If those become desirable,
purity can be layered later as an opt-in *on top of* statelessness, since the two are
compatible. This design does not attempt it.

## 4. Opt-in

There is no separate opt-in flag. The gate is how the library is defined:

- A library defined the new way (manifest class plus node metadata decorators, discovered
    via entry point) is on this contract.
- A legacy JSON-manifest library, loaded through the compatibility shim, keeps today's
    behavior: orchestrator-hosted in-process execution, or stub-plus-per-node-RPC if it is
    worker-flagged.

The shim boundary is therefore the contract boundary, which gives a clean property: libraries
migrate as a unit, and there is no mixed-conformance state inside a single library to reason
about. See `library-packaging-and-venues.md` for the definition mechanism itself.

## 5. Reentrancy: the one genuinely hard part

The orchestrator dispatches a subflow and awaits the result. The venue, while executing,
issues forwarded requests back to the orchestrator. The orchestrator must therefore service
those requests **while blocked on the dispatch it is awaiting**, or the system deadlocks.

Today this path exists but is an exception rather than the norm: forwarding is scoped to
`worker_node_execution_scope` (event_manager.py:861), and the current implementation already
carries substantial multi-event-loop hop machinery where cross-loop contention can stall for
seconds. Forwarding everything promotes this from edge case to hot path.

This needs to be designed, not inherited. Requirements:

- Servicing forwarded requests during an in-flight dispatch is a first-class, tested state,
    with its own concurrency model rather than ad-hoc loop hopping.
- Ordering guarantees must be stated: a node's forwarded mutations and its output
    publications must land in a defined order relative to each other.
- Failure modes must be bounded. A forwarded request that cannot be serviced should fail the
    execution with a diagnostic rather than hanging, even though liveness is otherwise
    maintained by heartbeats.
- The existing `reentrant-bus-in-init` hazard should be re-evaluated under the subflow model:
    because the orchestrator awaits a whole subflow rather than an individual instantiation, a
    request issued from `__init__` is expected to be serviceable normally.

## 6. License enforcement

This comes for free, and it improves on today.

`PermissionManager.pre_dispatch_hook` screens **every dispatched request** through the active
Cedar policy, registered generically on the engine's `EventManager`
(griptape-nodes-app: `retained_mode/managers/permissions/manager.py:193-208`), with
`authorize_checkpoint` (:458) covering non-request authorization checkpoints. There is no
execution-specific hook, and none is needed.

Because a stateless venue cannot call a manager directly, every state access it makes becomes
a dispatched request on the orchestrator and is therefore screened. That is strictly stronger
than the status quo, where an orchestrator-hosted node calls managers directly and those
calls never pass through request-level policy at all.

The venue needs no PermissionManager of its own, since it makes no policy decisions. Trust in
the venue process comes from provenance: the orchestrator spawns it, on the same machine,
from the same signed binary.

## 7. What gets deleted

- **Strict mode, entirely**: `common/strict_mode.py`, `common/strict_mode_checks.py`, the
    `STRICT_MODE` reporter singleton, the RUNTIME_EXECUTE and LOAD_PROBE scopes, the severity
    resolver, the `GTN_STRICT_MODE_DISABLED` escape hatch, and the `StrictModeViolationDetail`
    plumbing in result details. Every rule it carries describes a hazard that this
    architecture makes inexpressible: stub lossiness (venues run real nodes), parameter
    mutation not syncing (mutations are authoritative requests), worker reach into the
    orchestrator (now the sanctioned mechanism), and inert hooks (hooks run on real nodes).
- **The stub/schema-probe machinery**: superseded by the packaging design, which gives the
    orchestrator real node classes from a base-only import.
- **`FORWARDED_REQUEST_TYPES` as an enumeration**: the allowlist becomes unnecessary when the
    answer is "everything". `RemoteHandler` generalizes into the venue's default handler for
    all request types rather than a per-type swap.
- **Transient node materialization per request**
    (`_materialize_transient_node_from_metadata`): the venue instantiates real nodes for the
    subflow once and runs a real resolution machine over them.

## 8. Risks

1. **Latency from unconditional forwarding.** Every config read, secret read, and boundary
    input read is a round trip. The concrete hot spots are per-token streaming (18 files in
    the standard library stream via `append_value_to_parameter`, and Agent streams per token)
    and any loop that reads a parameter value repeatedly. Same-machine IPC makes this far
    cheaper than a network hop, but it is a real cost, accepted deliberately. Mitigations
    available later without changing the contract: batching outbound emissions, treating
    progress as fire-and-forget rather than awaited, and caching immutable reads for the
    duration of an execution.
1. **Reentrancy is the hot path** (section 5). This is the highest-risk engineering item.
1. **Same-machine-only forecloses distributed execution.** Running subflows on a separate GPU
    box or a cloud engine is out of scope by decision. Note that
    `library-packaging-and-venues.md` was written before this decision and still describes
    venues as local or remote, with an open question about distributing config and secrets to
    remote venues; that language predates and is narrowed by this document. The packaging doc
    is intentionally left unedited, so treat this section as the authority on venue location.
1. **Variable resolution interacts with forwarding.** `aprocess_scope(precomputed_variables)`
    exists specifically to avoid registry access during execution. Under forward-everything it
    is arguably redundant, but it is also the cheaper path; decide explicitly whether variable
    resolution stays pre-seeded or becomes another forwarded read.
1. **Hydration-time hooks can echo mutations.** On the venue, hydrating inputs fires
    `after_value_set`, which in libraries like diffusers recomputes dynamic parameter shape
    (for example `vae_decoder._update_output_parameter` swapping `output_image` for
    `output_video` plus `fps`). Those mutations would forward as requests for shape the
    orchestrator already computed editor-side. Hook-driven mutations during hydration need
    suppression, or every dispatch produces echoed parameter churn.
1. **Foreign graph reads race with parallel resolution.** Reads are authoritative but
    arbitrary in time: reading another node's value while it executes concurrently, or while
    a user edits it, is a race. Permitted by decision; the well-behaved pattern remains
    wiring a connection so the scheduler orders the dependency.
1. **Unbounded reach keeps libraries coupled to engine internals.** With the full surface
    available during execution, node code can depend on any request type, so libraries stay
    version-brittle across engine changes and third-party libraries are unattenuated (any
    library may request anything the orchestrator will service).

## 9. Work breakdown

1. **Venue runtime**: strip managers; implement the generic forwarding proxy as the default
    handler for all request types; boot path that imports libraries and runs a resolution
    machine over a dispatched subflow.
1. **Reentrancy design**: generalize `worker_node_execution_scope` into a tested
    service-while-awaiting model with stated ordering and bounded failures; retire the ad-hoc
    loop-hopping.
1. **Local value semantics**: venue-side resolution machine sets and reads in-subflow edge
    values locally; boundary values serialize.
1. **Hydration hook suppression**: distinguish hydration-driven from user-driven value sets
    so dynamic-shape hooks do not echo.
1. **Strict mode deletion**: remove both modules, the reporter, the scopes, and the
    result-details plumbing; remove the fitness problems that exist only to describe stub
    limitations.
1. **Forwarding generalization**: replace the `FORWARDED_REQUEST_TYPES` allowlist and
    per-type handler swap with a catch-all venue handler.
1. **Transient node removal**: delete per-request node materialization once subflow dispatch
    is the only path.
1. **Contract documentation**: author-facing statement of the rule ("your node is yours; the
    orchestrator owns everything else"), plus the latency guidance that follows from
    unconditional forwarding.
