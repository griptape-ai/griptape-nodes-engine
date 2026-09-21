# Venue Execution: Release 1 Plan

Status: proposal for team review. No code accompanies this document.

This is the buildable plan. `libraries-and-execution.md` describes the end-state architecture and
the reasoning behind each decision; this document scopes what ships first, in what order, and what
is deliberately deferred. Where the two differ on sequencing, this one is authoritative.

## 1. What changed, and why

The unified design bundled two changes: libraries become Python packages, and node execution moves
to stateless venues. Reviewing that with Collin surfaced a distinction worth acting on:
**packaging enables nothing.** Every benefit the design claims comes from the dependency split and
the venue model. Packages delete machinery and improve distribution, which is real value, but it is
cleanup of things that already work.

So packages come off the critical path. Release 1 keeps the JSON manifest, adds an exec-dep flag to
it, and builds the execution change. That inverts the obvious ordering, and the reason is
validation: **a packaging-first release cannot test what an execution release changes.** Authors
would port to packages, CI would go green, and every execution hazard would stay invisible because
their `process()` still runs in-process. Doing execution first means the risky part is proven before
the cosmetic part is attempted.

Three consequences worth stating up front:

- **Release 1 is backward compatible by default.** A library that changes nothing keeps all its deps
    as edit-time deps, loads as it does today, and runs in-process as it does today. Opting in means
    declaring which deps are exec-only. There is no compatibility shim, no legacy fence, no
    deprecation window, and no dual manifest source, because nothing about library declaration
    changes except one optional field.
- **The migration for an opting-in library is small.** Four items, section 4. No pyproject, no
    entry points, no manifest class, no decorators, no metadata migration.
- **The desktop env-manager workstream leaves release 1 entirely** (section 8).

## 2. Scope

**In:**

- Exec-dep declaration in the library JSON, and split installation.
- Cluster computation (union-find over unserializable edges and exec-dep sets).
- Stateless venue runtime with catch-all request forwarding.
- Cluster dispatch, with resolution-state-driven value residency.
- Boundary value contract with a codec registration mechanism.
- Reentrancy hardening for forwarded requests during an in-flight dispatch.
- Deletion of strict mode, the stub and schema machinery, and per-node RPC.
- First-party migration: diffusers as the proof point.

**Out, deferred to later releases:**

- Libraries as Python packages: pyproject as truth, entry-point discovery, code-derived metadata,
    distribution by requirement specifier.
- The single-resolution replacement venv, and the desktop app becoming an environment manager.
- Remote venues.
- Eliminating pickle at rest.

## 3. Architecture, briefly

Full detail is in `libraries-and-execution.md`. The parts release 1 depends on:

- **The orchestrator installs edit-time deps only** and therefore holds real node classes for every
    library, with real traits, converters, validators, and hooks. No stubs, no schema probe.
- **A venue** is a co-located engine process the orchestrator spawns, long-lived, holding edit-time
    deps plus the exec deps it hosts. Long-lived matters because a diffusion pipeline stays resident in
    VRAM across runs.
- **A venue is stateless**: no managers, no authoritative state. Every config read, secret read, graph
    query, graph mutation, and progress emission forwards to the orchestrator. One exception, storage,
    because a node saving a 4K video cannot ship bytes across the boundary; the venue writes through
    the storage driver locally and forwards only the registration and URL.
- **The dispatch unit is a cluster**, computed rather than authored:

```
1. Build the execution DAG for the control-flow step (existing DAG builder).
2. Label each node with the exec-dep set its library requires (empty if none).
3. Union two nodes across any edge whose parameter is serializable=False.
4. Optionally union adjacent nodes requiring the same non-empty exec-dep set.
5. A component with an empty exec-dep union executes in the orchestrator.
6. A component with a non-empty union dispatches to a venue holding that union.
7. If a component's union cannot co-resolve, fail with an error naming both libraries
   and the edge that forced them together.
```

This has to be automatic. Nodes joined by an unserializable value must co-locate for correctness,
which is a constraint users cannot see, and the 26 shipped diffusers templates contain no subflows
to hang it off. Because `Parameter.serializable` is static metadata, clusters are computable
without running anything, so the editor can display them and warn before a run.

- **Values inside a cluster are local**; only boundary values serialize. A live value that would have
    to cross venues is an error, not a spill-to-disk.
- **Across dispatches, residency follows resolution state.** Venues keep node instances; a dispatch
    carries a definition fingerprint and per-node resolution state; resolved means reuse the existing
    output, which is how both a live latent and a resident pipeline survive between dispatches. If the
    venue reports a value missing because it was reaped, the orchestrator marks that node unresolved
    and re-dispatches upstream, which is what makes idle reaping safe.
- **License enforcement gets stronger for free.** `PermissionManager.pre_dispatch_hook` screens every
    dispatched request (griptape-nodes-app `permissions/manager.py:193-208`). A stateless venue cannot
    call a manager directly, so every state access becomes a screened request, where today an
    in-process node's direct manager calls bypass request-level policy entirely.

## 4. What an opting-in library must do

1. **Declare which deps are exec-only.** A flag in `pip_dependencies` rather than a parallel list, so
    there is one place to look.
1. **Keep node modules importable with edit-time deps only.** Method-level imports are fine. Sibling
    exec modules are fine. **No layout is mandated**: the contract is the outcome, enforced by CI
    installing the library into a clean edit-only environment and importing every module. That is a
    real test rather than a proxy for one, it accepts either style, and it cannot be gamed.
1. **Make editor-time behavior work on edit-time deps.** This is the one genuinely structural item.
    Reflection over heavy classes has to become generated or declared data. Diffusers is the example:
    its dynamic parameters come from `inspect.signature(pipeline_cls.__init__)`
    (`parameters/modular_pipeline_type_parameters.py:23`) and `DiffusionPipeline._get_signature_keys`
    (:88), which cannot run without diffusers installed. The library's own CI introspects diffusers and
    emits a checked-in descriptor module, version-locked because it is generated against the exact pin.
    A genuinely custom pipeline falls back to a generic parameter set that populates on first execution
    through forwarded parameter mutations. Note also that artifact modules are usually the sharp edge
    rather than nodes: `artifact_utils/latent_artifact.py:6` imports torch at module scope and needs it
    behind `TYPE_CHECKING`.
1. **Convert manager access in execution paths to requests.** Direct accessors do not exist in a venue
    (section 5). This is the largest item by site count.

Explicitly not required: splitting a node across classes, splitting a node across modules, adopting
decorators, writing a manifest class, or publishing a package.

## 5. Manager access during execution: requests only

**Decided: requests only. No proxy shims.** During execution, node code reaches orchestrator state
through `handle_request`, never through a manager reference. Nothing is hidden: if it crosses the
boundary, it looks like it crosses the boundary.

This is the largest migration item in release 1, because direct accessors are common in execution
paths rather than confined to `__init__` and hooks. Driver config files call
`SecretsManager().get_secret(...)` while building drivers during process
(`config/prompt/openai_prompt.py:115`, `amazon_bedrock_prompt.py:149-151`, and every sibling).
`ConfigManager().workspace_path` appears in `seedance_common.py:516`, `video_capture.py:75`,
`save_latent_tensor.py:76`. Diffusers reads `enable_auto_resize` mid-encode in `vae_encoder.py:211`,
`vae_mask_encoder.py:139`, `noise_latent_node.py:150`, and in `utils/dimension_alignment.py:39`,
which is called from process paths.

Proxy shims were considered and rejected: they would have kept most process bodies untouched, but at
the cost of a property access secretly being an IPC round trip, invisible in review, so a loop reading
`workspace_path` is silently slow forever.

### 5.1 Enforcement: dummy managers

The venue installs a **dummy manager per manager type** whose attribute access raises a typed error
naming the manager, the attribute, and the request to use instead. `StaticFilesManager` stays real,
since storage is the one permitted local capability.

That is the whole mechanism. No static analysis: a lint cannot see dynamic access or calls made
several frames deep in helpers (`utils/dimension_alignment.py:39` reads config and is called *from*
process), and the identical call is legal in `__init__` and value hooks, so telling legal from illegal
would need call-graph analysis. Running the node raises; the message says what to do.

Finding sites to convert is a grep for `GriptapeNodes.<Manager>()`. Some hits are legal orchestrator-side
uses in `__init__` and hooks, and the library's own author knows which are which.

Implementation note: raise on attribute access rather than on call, so the traceback points at the right
line. Be aware a custom error from `__getattr__` makes `hasattr` propagate rather than return `False`,
which is the loud behavior we want but will surprise defensive code.

## 6. Build order

**Step 1: Reentrancy hardening.** Against today's workers, before anything depends on it. The orchestrator
must service forwarded requests while blocked awaiting a dispatch, and forwarding everything makes that
the hot path on machinery that currently stalls for seconds under cross-loop contention. Today it is an
exception scoped to `EventManager.worker_node_execution_scope` (event_manager.py:861), configured by
`configure_worker_forwarding` (:832), executed through `forward_to_orchestrator` (:925). Generalize it
into a tested service-while-awaiting model with an explicit concurrency model, stated ordering between a
node's forwarded mutations and its output publications, and bounded failure so an unserviceable request
fails the execution with a diagnostic rather than hanging. Independently valuable, and if it does not work
cleanly the architecture is in question.

**Step 2: Exec-dep declaration and split installation.** The JSON flag, and installation into two venvs per
library: edit-time deps spliced into the orchestrator exactly as today, exec deps spliced only inside a
venue. This deliberately reuses the existing per-library venv mechanism rather than replacing it
(section 8).

**Step 3: Venue runtime, clustering, and dispatch, proven on a fixture library.** Stateless venue with the
catch-all forwarding proxy and the dummy managers (section 5.1), union-find clustering, dispatch with
fingerprints and resolution state, missing-value recovery, boundary validation and codec registration.
Prove it against a synthetic library with a fake heavy dependency and a fake unserializable artifact
**before** touching diffusers, so the mechanism is not gated on the hardest migration.

**Step 4: Diffusers migration.** Import placement, artifact modules base-clean, the generated descriptor
table, and whatever section 5 requires. Now there is a working mechanism to validate against instead of
porting on faith. Also fix issue #2511 so local parameter removal cascades connection removal, which
deletes the `remove_parameter_element_by_name` overrides, and make hydration-driven value sets
distinguishable from user-driven ones so `_update_output_parameter` does not echo mutations on every
dispatch.

**Step 5: Demolition.** Strict mode in full (`common/strict_mode.py`, 336 lines;
`common/strict_mode_checks.py`, 198 lines; the reporter, both scope kinds, the severity resolver, the
disable environment variable, and the violation plumbing in result details). The stub and schema machinery
(`_make_worker_stub_class` :4451, `_register_nodes_from_worker_schemas` :4401,
`_serialize_library_node_schemas` :5032, the probe timeout :4926, `WorkerNodeSchema` and the
`LibraryLoadedNotification.node_schemas` field it types at app_events.py:267). Per-node RPC
(`_execute_node_via_worker` node_manager.py:3226, `_materialize_transient_node_from_metadata` :3174,
`CancelExecuteNodeRequest`, the `FORWARDED_REQUEST_TYPES` allowlist and per-type `RemoteHandler` swap at
app/worker_routing.py:105, :239, :287, and `common/parameter_hydration.py` which the codec contract
supersedes, closing issue #4475). The Shared-versus-Isolated machinery (`worker_mode_override`
settings.py:125, the `WorkerModeCompatibility` and `SuggestedWorkerMode` declarations with
`requires_worker_process()` library_declarations.py:243-265, and `_resolve_requires_worker` :5310-5358),
since placement is now derived from exec-dep presence. And `SubprocessWorkflowExecutor` with its subprocess
script and websocket listener, absorbed into venue dispatch.

Order note: steps 1 and 2 are independent and can run concurrently, 3 depends on 2, 4 depends on 3, and
5 depends on 4.

## 7. Verification

- **Isolation proof**: the orchestrator environment contains no torch while every diffusers node still
    instantiates, displays, serializes, and round-trips through save and load; editor-time dynamic parameters
    work with no venue running.
- **Cluster proof**: union-find produces the expected grouping for each shipped diffusers template with no
    authoring changes; exec-dep sets that cannot co-resolve within one cluster error with both libraries and
    the edge named.
- **Latent proof**: a multi-node diffusers cluster runs in a venue passing live tensors in-process, with
    progress streaming to the GUI and outputs landing on the orchestrator.
- **Residency proof**: a multi-stage template hands a latent between dispatches (`MultistageText2Image` and
    `LTX23-HDR-Text2Video-Upsample-Two-Stage` each reference latents 18 times); re-running with a changed seed
    reuses the resident pipeline without reloading it; reaping the venue mid-workflow triggers missing-value
    recovery and re-execution rather than a wrong result.
- **Boundary proof**: a non-serializable value on a cluster boundary fails at dispatch naming the parameter.
- **Forwarding proof**: a node performing config, secret, static-file, and graph-mutation access during
    execution succeeds; the same access is screened by license policy; reentrant service during an in-flight
    dispatch is exercised under contention with measured latency and no deadlock.
- **Compatibility proof**: an untouched library, including a heavy one, loads and executes exactly as before.
- **Platform**: the matrix on Windows, with attention to long paths and file locking.

## 8. What is deferred, and why it is safe

**The single-resolution replacement venv, and the desktop env manager.** This was the largest schedule risk
in the unified plan, and the exec split makes it largely unnecessary for release 1. The pins that actually
collide are torch, transformers, diffusers, and CUDA wheels, and all of them move to exec, which is
per-venue and never shared. What remains in the shared edit-time environment is pillow, numpy, and griptape,
which can collide in principle and rarely do. So release 1 keeps today's per-library venv and splice for
edit-time deps and gets most of the collision benefit with none of the cross-repo coordination. Deferring it
means edit-time deps can still shadow each other, which is today's behavior, not a regression.

**Packages.** Worth doing, and worth doing later. They delete the synthetic module loader with its hashed
dynamic names, the stable-namespace alias machinery and its meta-path finder, and the module-path patching
that saved workflows need for pickled values. They stop the hand-rolled reinvention of package management:
git cloning, ref switching, version pinning, update checks, minimum-release-age gating, provisioning, and
transitive library-to-library resolution over git URLs. They give real version solving across the whole
graph, and PyPI distribution. All of it is cleanup of working machinery, and none of it unblocks the
execution model. Deferring it means that machinery survives, and top-level module name collisions between
libraries persist, since two libraries both shipping a `utils/` package still clash when their directories
are on `sys.path`. That is a bug that exists today.

**Remote venues.** Out of scope, and not foreclosed. Version-locking both sides is accepted policy, which
removes the obstacle that would have been most expensive to retrofit. Three constraints observed in release 1
keep the option open at no cost: all file access goes through the storage driver rather than raw shared
paths, forwarding stays behind the transport-agnostic seam `WorkerManager` already provides via
`attach_transport`, and dispatch payloads stay self-contained. What remote would additionally need is
mandatory latency mitigations, venue authentication, and partition handling. See
`libraries-and-execution.md` section 8.

## 9. Risks

1. **Reentrancy becomes the hot path.** Highest-risk item, which is why it is step 1 and measured before
    anything depends on it.
1. **Forwarding latency.** Every config read, secret read, and boundary input re-read is a round trip, with
    hot spots in per-token streaming (18 standard-library files stream, Agent per token) and loops that
    re-read values. Same-machine IPC keeps this cheap. Mitigations that do not change the contract: batch
    outbound emissions, make progress fire-and-forget rather than awaited, cache immutable reads for an
    execution.
1. **VRAM pressure from long-lived venues.** Intermediate values sit on node instances until recomputed,
    which is today's behavior, but concurrent runs across workflows multiply resident memory. Needs an
    admission cap or per-venue serialization, with idle reaping as the release valve.
1. **Diffusers is the only real proof point.** It is the sole first-party heavy library, so the design gets
    validated against one migration. The fixture library in step 4 partially compensates by exercising the
    mechanism independently.
1. **Cluster granularity can surprise users.** One unserializable edge silently merges two regions into a
    single venue, which is correct but can produce a larger dispatch unit than expected. Editor visualization
    is the mitigation.
1. **Edit-time deps can still shadow each other**, since the splice remains for them. Bounded by those deps
    being light, and fixed later by the single resolution.
1. **Foreign graph reads race with parallel resolution.** Permitted by decision; reads are authoritative but
    arbitrary in time. Wiring a connection remains the well-behaved pattern.
1. **Unbounded reach keeps libraries coupled to engine internals.** Version-locking is accepted policy, which
    makes this a maintenance cost rather than a compatibility hazard.

## 10. Open questions

1. **Variable resolution under forwarding.** `aprocess_scope(precomputed_variables)` (node_types.py:135)
    exists so an executing node needs no registry access for substitutions. Under forward-everything it is
    arguably redundant but also cheaper. Decide whether it stays pre-seeded or becomes a forwarded read.
1. **Ergonomics of request-only access.** Converting `SecretsManager().get_secret(name)` into a request is
    more verbose at every call site, across a lot of sites. Whether to add thin conveniences on `BaseNode`
    that issue the request internally is worth deciding *before* the migration, since it determines what all
    the converted code looks like. Note this is not a shim by the back door: a method on the node is honest
    about being node API, where a manager reference implies local state that is not there.
1. **Venue consolidation.** Whether to merge compatible exec-dep sets into fewer venues to save memory, or
    keep them separate for isolation and simpler reaping.
1. **Cluster visualization design.** The mechanism produces the grouping; how it reads on the canvas is
    undesigned.
1. **Concurrent run admission.** Whether venues serialize runs, cap concurrent sessions, or defer to an
    orchestrator-level scheduler.
1. **Boundary codec scope for release 1.** Whether to ship a full registration mechanism or start with
    griptape artifacts plus a clear error, deferring third-party codecs.
