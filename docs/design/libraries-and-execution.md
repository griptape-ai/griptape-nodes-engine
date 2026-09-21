# Libraries as Packages, Execution in Venues

Status: proposal for team review. No code accompanies this document.

**Read this for the end-state architecture. Read `venue-execution-plan.md` for what actually
ships first.** That plan supersedes this document on sequencing: packages have come off the
critical path, release 1 keeps the JSON manifest with an exec-dep flag added, and the
single-resolution replacement venv is deferred. The architecture below still describes where
this is going, and the reasoning for each decision holds.

This document supersedes `library-packaging-and-venues.md` and `node-execution-contract.md`,
which were written as two sequential efforts. Those remain useful for the reasoning behind
individual decisions; where either conflicts with this one, this one is authoritative on
architecture.

The end state is one coherent system rather than two phases, which is why section 1.2
enumerates the transitional machinery that a phased packaging-then-execution rollout would
have required. Note that the plan document reaches the same place by a different route: it
ships execution first and treats packaging as follow-on cleanup, which avoids that machinery
for a different reason.

## 1. Summary

### 1.1 What changes

A library stops being a folder with a JSON manifest and becomes an ordinary Python package.
Its dependencies split in two: a light **base** set sufficient to import every node module,
instantiate every node, and drive every editor-time behavior, and one **execution extra**
carrying the heavy deps that only `_process` needs. The orchestrator installs base packages
for every registered library, so it holds real node classes with real traits, converters,
validators, and hooks, and can edit, serialize, and display any graph without importing torch.

Execution of anything needing heavy deps moves to a **venue**: a co-located engine process the
orchestrator spawns, holding the orchestrator's resolved base environment plus the execution
extras it hosts. Venues are long-lived, so an expensive resident resource such as a diffusion
pipeline stays warm in VRAM across runs.

A venue receives a **cluster** of nodes that must run together, computed automatically from the
graph (section 3.5), and runs its own resolution machine over real node instances. It holds no
authoritative state: every config read, secret read, graph query, graph mutation, and progress
emission forwards to the orchestrator. Live values such as a `LatentArtifact` flow between
co-located nodes inside the venue; only cluster boundary values serialize.

Because the venue holds no second copy of anything the orchestrator owns, divergence between an
executing node and its authoritative node cannot occur, and the framework that exists to police
that divergence is deleted rather than adapted.

### 1.2 What executing as one piece eliminates

Each of these existed solely to make packaging shippable before venues existed:

- **The WorkerV1 coexistence bridge.** No worker-side execution extra install, no retained
    per-library venv and sys.path splice on the worker path, no engine-dist exclusion problem.
- **Transitional execution extras in the orchestrator environment.** The orchestrator is
    base-only from the first release, not eventually.
- **Stub coexistence.** The orchestrator never holds stub classes, so `WorkerNodeSchema`, the
    schema probe, and the probe timeout are deleted outright rather than per-library.
- **The three-way split of strict-mode rule deletion.** The framework goes in one pass.
- **A two-step migration for library authors.** A library is ported once.
- **The Shared versus Isolated user-facing choice.** Placement is derived, so
    `worker_mode_override`, `WorkerModeCompatibility`, `SuggestedWorkerMode`, and
    `requires_worker` all disappear (section 4.4).

### 1.3 What it costs

One large cutover in a single major version, with no intermediate release delivering part of the
value. Verification is therefore deliberate rather than incremental (section 10). The risk
concentration is real and is the principal argument against this sequencing; it is accepted in
exchange for never building the bridge machinery above.

## 2. Problem

Two halves of one problem.

**Dependencies.** Deps are declared in `griptape_nodes_library.json` and duplicated into
`pyproject.toml`, the only sync being a per-library Makefile target that nothing verifies and the
engine never reads. Per-library venvs exist, but every shared library's site-packages is spliced
onto the single orchestrator `sys.path` (`_add_library_paths_to_sys_path`,
library_manager.py:2965) with `sys.path.insert(0, ...)`, so the last library loaded shadows
everyone else's copy of a package. Library A and Library B can be mutually exclusive, and the
failure surfaces at runtime rather than at install.

**Execution.** `process()` mutates node state and reaches into engine internals mid-execution.
Running a node off the orchestrator therefore requires materializing a transient stub node
(`_materialize_transient_node_from_metadata`, node_manager.py:3174), stateless between requests
by construction (:3111), with an orchestrator-side stub class synthesized from a worker-probed
schema (`_make_worker_stub_class`, library_manager.py:4451). The stub carries parameter shape
only: no converters, no validators, no traits, no hooks. Strict mode, 534 lines across
`common/strict_mode.py` and `common/strict_mode_checks.py`, exists to report the resulting
fidelity losses at runtime.

The halves interlock. Isolation is the answer to dependency collisions, but isolation is only
available through the lossy stub path, so the libraries that most need it are the ones least able
to use it. Diffusers is the proof: it declares no worker mode, passes live tensors between nodes,
and therefore runs in the orchestrator with its full torch stack on the shared `sys.path`.

## 3. Target architecture

### 3.1 A library is a Python package

```toml
[project]
name = "griptape-nodes-library-example"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = [
  # BASE: enough to import every node module, instantiate every node,
  # and run every editor-time behavior.
  "griptape-nodes-engine>=0.99,<0.110",
  "pillow>=11.2.1",
]

[project.optional-dependencies]
# One execution extra. Its presence is what makes this library venue-executed.
exec = ["torch==2.7.0", "diffusers==0.39.0"]

[project.entry-points."griptape_nodes.library"]
example = "example_library.library:LIBRARY"

[tool.griptape-nodes]
# Resolve arguments that must be known before install (section 5.2).
index-url = "https://download.pytorch.org/whl/cu128"
torch-backend = "auto"
```

Layout:

```
example_library/
  library.py                  # LibraryManifest subclass, exported as LIBRARY
  nodes/generate.py           # base-clean: class definitions, parameters, hooks
  exec/generate_exec.py       # heavy logic, normal top-of-file imports
  widgets/, workflows/        # package data, included in the wheel
```

`_process` performs one lazy import of the exec module and delegates:

```python
class Generate(BaseNode):
    def process(self) -> None:
        from example_library.exec import generate_exec

        generate_exec.run(self)
```

### 3.2 Metadata from code, and the library-side obligation

The existing pydantic models survive unchanged (`LibraryMetadata`, `NodeMetadata`,
`CategoryDefinition`, `Setting`, `WidgetDefinition`, and the declaration union). Only their
source moves from JSON to code.

A decorator annotates each node class, validating through `NodeMetadata` at import so authoring
errors surface at load rather than at first canvas drop. It annotates only, with no registry side
effects, so import order is irrelevant and harvesting is pull-based.

```python
@node_metadata(category="latents", display_name="Generate Latents", icon="Sparkles")
class Generate(BaseNode): ...
```

A manifest class carries library-level metadata and absorbs `AdvancedNodeLibrary`, so there is
one authoring surface instead of a JSON file plus an optional advanced module:

```python
class ExampleLibrary(LibraryManifest):
    name = "Example Library"
    metadata = LibraryMetadata(author=..., description=..., tags=[...])
    categories = {"latents": CategoryDefinition(...)}
    settings = [Setting(category="example_library", contents={...})]
    widgets = [WidgetDefinition(name="AnglePicker", path="widgets/AnglePicker.js")]

    def get_node_modules(self) -> list[str]:
        return default_node_module_walk(__package__, exclude=("exec",))

    def before_library_nodes_loaded(self, ...): ...
    def get_request_handlers(self): ...

LIBRARY = ExampleLibrary()
```

`library_version` and `engine_version` leave the metadata model: version comes from
`importlib.metadata`, engine compatibility from the dependency pin. The `Dependencies` model
(library_registry.py:64) is deleted. `LibrarySchema` survives as the internal registry record,
constructed by the manifest rather than parsed from disk. Import posture is eager, since base
modules are cheap by contract and eager import removes the lazy-entry footguns in `NodeTypeEntry`
(library_registry.py:536).

**The base-clean contract covers editor-time behavior, not just module imports.** Manifest hooks,
`get_request_handlers()`, `get_post_dispatch_hooks()`, converters, validators, traits, value and
connection hooks, and anything reachable from a saved workflow's embedded values must all be
importable and functional with base deps only. There is **no `[exec]` access at edit time, ever.**

**This is a library-side obligation. The engine does not grow a mechanism per library.** If a
library's editor-time behavior currently needs heavy deps, that library restructures. The rule
matters because the alternative is a platform that accumulates one-off accommodations.

*Worked example, diffusers.* Its dynamic parameters are built by reflection over the pipeline
class: `inspect.signature(pipeline_cls.__init__)`
(parameters/modular_pipeline_type_parameters.py:23) and `DiffusionPipeline._get_signature_keys`
(:88). That cannot run on a base-only orchestrator. The library's own CI introspects diffusers and
emits a **checked-in descriptor module that ships in the base package**, which the engine sees as
an ordinary base-clean module. The table is version-locked because CI generates it against the
exact pin in `[exec]`. For a genuinely custom pipeline the table has no entry, and the node ships
a generic parameter set that populates on first execution through **forwarded parameter
mutations**, which the design already sanctions. Degraded authoring for an inherently unknown
pipeline, and zero engine surface added. Note also that the library's artifact modules are the
sharp edge of the split, not its nodes: `artifact_utils/latent_artifact.py:6` imports torch at
module scope, and must move it behind `TYPE_CHECKING` so the class is base-importable while only
exec code constructs instances.

A saved value whose class lives behind the exec boundary degrades gracefully, dropping to default
with a load warning and marking the node unresolved, rather than failing the workflow open.

For libraries a user has not installed, the wheel carries a **build-time derived-metadata
artifact**: CI imports the base package and dumps derived node and category metadata into the
distribution, serving the library directory and register-by-specifier UI without importing
anything.

### 3.3 Dependency model: one resolution, extended per venue

**Orchestrator environment.** A single replacement venv containing the engine, the app, and every
registered library's base packages, resolved together, from which the engine process runs. A base
dep that cannot co-resolve fails at registration with the offending library named, rather than
silently shadowing at import.

**Venue environment.** The orchestrator's resolved base environment plus the execution extras the
venue hosts. Deliberately not an independent resolution: the venue extends the orchestrator's
lockfile rather than computing its own.

Consequences:

- Base packages are identical version-for-version on both sides, so **any node can be instantiated
    in any venue**. Section 3.5 depends on this.
- The engine and app dists come from the orchestrator's environment rather than being reinstalled,
    so no second engine copy can shadow the running one.
- Execution extras never enter the orchestrator's resolution, so torch-class pins are structurally
    absent from the environment the editor runs in. This is what makes collisions impossible rather
    than merely loud.
- Two libraries whose execution extras cannot co-resolve simply get different venues. They conflict
    only if forced into one, which happens exactly when their nodes land in one cluster.

### 3.4 Execution model: stateless venues

A venue is an engine process, spawned by the orchestrator, on the same machine, and long-lived.

**Stateless** means it holds no authoritative, orchestrator-owned state: no ConfigManager, no
SecretsManager, no ObjectManager, no FlowManager, no registry authority. It is not stateless in the
trivial sense. It legitimately holds Python module state, real node instances for its clusters, and
heavy resident resources such as a pipeline in VRAM. The rule is about authority, not memory:
compute with whatever you need, be the source of truth for nothing.

**Everything forwards.** Config reads, secret reads, graph queries, graph mutations, object lookups,
progress and log emission, parameter output publication, status updates. The venue implements no
local handler; it proxies. The proxy is generic, deserializing a request, shipping it, awaiting the
result, and returning it without ever interpreting what it means, which is why forwarding the whole
retained-mode surface does not require the venue to reimplement the engine.

**One explicit exception: storage.** A venue holds a local `StaticFilesManager`, because a node
saving a 4K video cannot ship bytes across the boundary. Bytes are written in-process through the
storage driver; only the resulting registration and URL forward. This is the sole permitted local
manager and must stay stated, since leaving it implicit is how someone later decides to forward the
bytes. **File access always goes through the storage driver abstraction, never raw shared paths**,
which costs nothing today because node code already calls `save_static_file`.

**Co-location is the initial deployment target, not an architectural assumption.** Section 8 covers
remote engines and what preserving that option requires.

### 3.5 Clusters: computed, not authored

Nodes connected by an unserializable value **must** run in the same venue. That is a correctness
constraint, not a preference, so it cannot be delegated to the user: asking someone to draw a
boundary means asking them to solve a constraint they cannot see, and a boundary drawn one node too
early is a hard error. It also would require re-authoring every existing workflow, since the 26
diffusers templates contain no subflows at all.

The constraint that makes this necessary also makes it computable, from data the engine already has.
`Parameter.serializable` is static metadata, so unserializable edges are known without running
anything.

```
1. Build the execution DAG for the control-flow step (existing DAG builder).
2. Label each node with the extras set its library requires (empty for base-only libraries).
3. Union two nodes across any edge whose parameter is serializable=False.
4. Optionally union adjacent nodes requiring the same non-empty extras set, to avoid
   pointless boundary crossings.
5. A component whose extras union is empty executes in the orchestrator.
6. A component whose extras union is non-empty is a dispatch unit. Its venue environment is
   the orchestrator's resolved base environment plus that union.
7. If a component's extras union cannot co-resolve, fail with an error naming both libraries
   and the edge that forced them together.
```

Properties:

- Existing workflows work untouched.
- A lone heavy node is a component of size one, so it needs no special rule.
- A base-only passthrough node sitting in a latent chain is absorbed into the cluster, and *runs*
    there because the venue environment contains base for everything (section 3.3).
- Because it is statically computable, the editor can **show** clusters and warn before running.
    The UX becomes visualizing a derived fact rather than authoring a constraint.
- A user-authored `SubflowNodeGroup` still means grouping and can hint an execution environment, but
    it is not the co-location mechanism. A user boundary that would cut an unserializable edge is
    rejected with a clear message rather than failing at dispatch.

### 3.6 Value semantics and residency

**Within a cluster, values are local.** The venue's resolution machine sets and reads them
in-process. This is correctness, not optimization: a `LatentArtifact` wraps a live tensor and is
deliberately unserializable, so a forwarded read would require the orchestrator to hold something
that cannot cross a process boundary.

**At cluster boundaries, values serialize**, and are serializable by definition because they
crossed. The contract is JSON-safe scalars plus registered artifact codecs that live in libraries'
*base* packages so both sides can decode. No pickle crosses the boundary. A non-serializable value
on a boundary parameter is a dispatch-time validation error naming the parameter.

**Live values that must cross venues are simply an error.** There is no spill-to-disk mechanism.
Chaining a torch 2.7 library to a torch 2.4 library through a live tensor is not expressible, and
the design says so rather than half-supporting it.

**Across dispatches, residency is governed by resolution state.** Live values need to survive
between dispatches: multi-stage workflows hand latents between stages (`MultistageText2Image` and
`LTX23-HDR-Text2Video-Upsample-Two-Stage` each reference latents 18 times), and
`Multi-ViewPromptBatcher` iterates. Rather than inventing a value-handle abstraction or run-scoped
sessions, the venue mirrors the discipline the orchestrator already uses for its own long-lived
nodes, where values persist on node instances and correctness comes from resolution state.

- Venues are long-lived and keep node instances per cluster.
- A dispatch carries the cluster spec, boundary inputs, an execution id, a **definition
    fingerprint**, and **per-node resolution state**.
- A node marked resolved means the venue reuses its existing output. This is how a live latent, or a
    resident pipeline, survives between dispatches.
- A node marked unresolved recomputes, replacing its prior output.
- A fingerprint mismatch rebuilds the cluster's instances, correctly discarding their values because
    the definitions changed.
- If the venue reports a value missing, because it was reaped or restarted, the orchestrator marks
    that node unresolved and re-dispatches from further upstream. **This is what makes idle reaping
    safe**, reducing it to cache eviction whose worst case is reloading a model.

Run-scoped sessions were rejected: freeing intermediates at run end would also free the pipeline,
since the pipeline is itself a node output, which defeats the entire reason venues are long-lived.
Exempting "expensive" values would require inventing a taxonomy of value importance, which is worse
than reusing resolution state.

The authority rule, sharpened: the orchestrator is authoritative for definitions and resolution
state, and the venue is a **cache for values only it can hold**. Those are precisely the values the
orchestrator could never represent, so nothing left its ownership.

### 3.7 License and policy

This comes for free, and it strengthens.

`PermissionManager.pre_dispatch_hook` screens every dispatched request through the active Cedar
policy, registered generically on the engine's `EventManager` (griptape-nodes-app
`retained_mode/managers/permissions/manager.py:193-208`), with `authorize_checkpoint` (:458)
covering non-request checkpoints. There is no execution-specific hook and none is needed.

Because a stateless venue cannot call a manager directly, every state access it makes becomes a
dispatched request on the orchestrator and is therefore screened. That is strictly stronger than the
status quo, in which an orchestrator-hosted node calls managers directly and those calls never pass
through request-level policy at all. The venue needs no PermissionManager of its own since it
decides nothing; trust comes from provenance, as the orchestrator spawns it on the same machine from
the same signed binary.

## 4. Decisions

### 4.1 Packaging

| Topic             | Decision                                                                                                                                                                   |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Manifest          | `griptape_nodes_library.json` dies. pyproject.toml is package-level truth: deps, extras, entry point, static `[tool.griptape-nodes]` resolve-args table.                   |
| Metadata          | Code-derived, via manifest class plus node decorator. Decorators annotate only. Uninstalled-library listing served by a build-time derived-metadata artifact in the wheel. |
| Extras            | One execution extra per library, convention name `exec`. Its presence determines placement.                                                                                |
| Base-clean scope  | Covers all editor-time behavior, not just module imports. No `[exec]` access at edit time. Libraries restructure; the engine adds no per-library mechanism.                |
| Import discipline | Node modules base-clean; heavy logic in `exec/`; `_process` lazily imports and delegates. CI-lintable.                                                                     |
| Orchestrator env  | One full replacement venv: engine, app, all base packages resolved together; the engine runs from it.                                                                      |
| Venue env         | The orchestrator's resolved base environment plus hosted execution extras. Not an independent resolution.                                                                  |
| Env ownership     | `LibraryEnvironmentManager` is engine-side; the desktop app is one packaging of it, never the owner.                                                                       |
| Distribution      | Any requirement specifier: PyPI, git+https, local path, editable.                                                                                                          |
| Engine compat     | The engine is a normal base dependency with a bounded range, enforced by the resolution.                                                                                   |
| Import posture    | Eager.                                                                                                                                                                     |
| Compat shim       | Legacy JSON libraries are fenced, not integrated (section 5.8). Time-boxed.                                                                                                |

### 4.2 Execution

| Topic                    | Decision                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| State model              | Venues are stateless: no managers except a local storage driver, no authoritative state.                                      |
| Forwarded surface        | Everything. The proxy is generic and never interprets a request.                                                              |
| Storage                  | Local capability. Bytes written in-process through the driver; only registration and URL forward. Never raw shared paths.     |
| Dispatch unit            | A cluster, computed by union-find over unserializable edges and extras sets. Per-node dispatch does not exist.                |
| Cluster authoring        | Automatic. User groups are hints, never the co-location mechanism.                                                            |
| Intra-cluster values     | Local, by the venue's own resolution machine.                                                                                 |
| Boundary values          | Serializable, via base-package artifact codecs. No pickle on the wire.                                                        |
| Cross-venue live values  | An error. No spill mechanism.                                                                                                 |
| Cross-dispatch residency | Governed by resolution state on long-lived venue node instances, with a definition fingerprint and missing-value recovery.    |
| Venue lifetime           | Long-lived, orchestrator lifetime, keyed on extras set, with idle reaping made safe by missing-value recovery.                |
| Venue location           | Same machine initially. Remote preserved as a future mode (section 8).                                                        |
| Foreign graph access     | Allowed; forwarded state is always authoritative.                                                                             |
| Placement                | Derived from execution-extra presence, never declared or user-toggled.                                                        |
| Version policy           | Orchestrator and venues are version-locked and ship together. Accepted deliberately, including for future remote deployments. |
| Strict mode              | Deleted.                                                                                                                      |
| License                  | Free via the request pre-dispatch hook, and stronger than today.                                                              |

### 4.3 Considered and rejected

- **`process()` as a pure function** (values in, values out, opt-in per node). The original framing
    of the execution half. Statelessness achieves the same goal, correctness by construction, without
    a migration tax across 242 `process`/`aprocess` bodies, 24 files using the `AsyncResult` yield
    pattern, and 18 files streaming mid-execution. Recorded so it is not silently reintroduced: purity
    remains compatible with statelessness and could be layered later as an opt-in earning engine-free
    unit testing and result memoization. It is not in this design, and consequently neither benefit is.
- **An enumerated forwarding surface.** Rejected because a stateless venue is a generic proxy, so
    surface size does not constrain its implementation.
- **User-authored subflow boundaries as the co-location mechanism.** Rejected: the constraint is
    invisible to users and every existing workflow would need re-authoring.
- **Value handles** (orchestrator holds `{id, type, preview}`, venue holds the value). Rejected in
    favor of resolution state, which needs no new abstraction, no value store, and no GC protocol.
- **Run-scoped venue sessions.** Rejected because freeing intermediates at run end also frees the
    resident pipeline.
- **Spill codecs** for values crossing venues. Rejected; that case is an error.
- **A venue definition-query mechanism** for edit-time introspection. Rejected in favor of the
    library-side obligation, which keeps the engine free of per-library accommodations.
- **A constraint-locked overlay venv** rather than a full replacement venv. Retained as the fallback
    if desktop env-manager work proves harder than expected.
- **Independent venue resolution.** Rejected in favor of extending the orchestrator's lockfile.
- **Two sequential phases.** See 1.2 and 1.3.

### 4.4 Deleted concepts

Shared versus Isolated as a user-visible choice, and its machinery: `worker_mode_override`
(settings.py:125), the `WorkerModeCompatibility` and `SuggestedWorkerMode` declarations with
`requires_worker_process()` (library_declarations.py:243-265), `LibraryInfo.requires_worker`, and
`_resolve_requires_worker` (library_manager.py:5310-5358). Placement is a fact about dependencies,
not a preference. Forcing in-process execution for debugging, if wanted, is a developer setting
rather than a library declaration.

`LibraryDependencyDeclaration` (library_declarations.py:211) and its git-URL transitive resolution:
library-to-library dependencies become ordinary package dependencies.

## 5. Mechanics

### 5.1 Environment management

`LibraryEnvironmentManager`, engine-side, owns resolve, build, and staged rebuild.

- **Inputs**: registered specifiers from config, plus each library's `[tool.griptape-nodes]` resolve
    args. **Outputs** beside the environment: `requirements.in`, `resolution.lock`, and the resolved
    extras sets per venue.
- **Apply model**: hot-install for pure additions where nothing already installed changes version.
    Otherwise stage a fresh environment and swap on restart, because Python cannot un-import. The
    existing `_libraries_reloaded_after_import` scar tissue is the evidence for not attempting
    in-place mutation.
- **Restart choreography**: the engine runs from the environment being replaced. On desktop the app
    respawns it through the existing lifecycle. Headless, CLI, and container deployments re-exec into
    the staged environment's interpreter, with operator-restart as the documented fallback.
- **Failure UX**: bisect on failure (each library alone, then pairs) and emit a typed
    `DependencyResolutionConflictProblem(library, requirement, conflicts_with, uv_stderr)`. The
    offending library fails and the remainder is re-resolved, so one bad library never blocks startup.
- **Keying**: engine and Python version. An engine upgrade rebuilds from `requirements.in` on first
    boot, isolating per-library failures so an upgrade never bricks the node picker.
- **File-level collision guard**: two dists shipping the same top-level import package into one
    environment is last-write-wins for the installer, which is silent shadowing at a different layer.
    A registration-time overlap check across installed dists' RECORD files catches it, backed by a
    template lint requiring a unique library-named top-level package.

### 5.2 Resolve arguments that must precede install

`[tool.uv.sources]` and `[[tool.uv.index]]` are project configuration, honored when resolving a
library's own repo and ignored when the library installs as a dependency, which is every install
path here. Index and backend requirements are therefore data the environment manager reads *before*
resolving, merged resolve-wide with a loud warning on cross-library conflict.

The carrier is the static `[tool.griptape-nodes]` table, readable with `tomllib` from an sdist, git
checkout, or editable path. **Wheels do not contain pyproject.toml**, only package files and
`.dist-info/`, so for wheel-distributed libraries the authoritative pre-resolve read is the
build-time derived-metadata artifact inside the wheel, which mirrors the table at build time. A
per-registration config override exists as an escape hatch.

### 5.3 Venue lifecycle

The existing worker machinery is the substrate: spawn, register, heartbeat, evict, and route are
reused nearly verbatim, renamed from worker to venue, with exact version matching retained since
both sides ship together.

- A venue is keyed on its **extras set**, not a single library, which is what lets one venue host a
    multi-library cluster.
- Lifetime is long-lived by default, with ephemeral and pinned available via library metadata, and
    idle reaping driven by the existing heartbeat clock. Reaping is safe because of missing-value
    recovery (section 3.6).
- Spawn is lazy: the first dispatch requiring an extras set materializes the venue, including its
    environment extension if not already built.
- Dispatch with no live venue either queues until ready or fails with an explicit
    execution-unavailable status. It never falls back to in-process execution, since the orchestrator
    lacks the deps by design. The engine already hard-raises in this situation
    (`get_worker_for_library`, library_manager.py:768-792), and that guard must be preserved rather
    than reintroduced.

### 5.4 Dispatch protocol

`SubflowNodeGroup.execution_environment` (common/node_executor.py:250-264) becomes the venue
selector for user-hinted groups, and the existing PRIVATE_EXECUTION path is absorbed rather than
paralleled: its packaging step, `SubprocessWorkflowExecutor`, and event relay are replaced by venue
dispatch.

- `ExecuteClusterRequest{cluster_spec, boundary_inputs, execution_id, definition_fingerprint, node_resolution_states}`.
- The venue reuses or rebuilds node instances per the fingerprint, honors resolution state, runs its
    resolution machine, streams execution events for GUI relay, and returns
    `ExecuteClusterResult{boundary_outputs, errors, missing_values}`.
- `CancelClusterRequest(execution_id)` replaces per-node cancellation.
- No wall-clock timeout on execution, consistent with today; liveness is the heartbeat's job.

### 5.5 Forwarding and reentrancy

This is the hardest part of the design and the highest-risk item to build.

The orchestrator dispatches a cluster and awaits the result. The venue, while executing, issues
forwarded requests back, which the orchestrator must service **while blocked on the dispatch it is
awaiting**, or the system deadlocks.

That path exists today as an exception: forwarding is scoped to
`EventManager.worker_node_execution_scope` (event_manager.py:861), configured by
`configure_worker_forwarding` (:832), executed through `forward_to_orchestrator` (:925), and gated
by the `FORWARDED_REQUEST_TYPES` allowlist with per-type `RemoteHandler` swaps
(app/worker_routing.py:105, :239, :287). The current implementation carries substantial
multi-event-loop hop machinery where cross-loop contention can stall for seconds. Forwarding
everything promotes this from edge case to hot path.

Requirements:

- Servicing forwarded requests during an in-flight dispatch is a first-class, tested state with an
    explicit concurrency model, not ad-hoc loop hopping.
- Ordering is stated and enforced: a node's forwarded mutations and its output publications land in
    a defined order relative to each other.
- Failures are bounded: a forwarded request that cannot be serviced fails the execution with a
    diagnostic rather than hanging.
- The allowlist is replaced by a catch-all venue handler.
- The historical `reentrant-bus-in-init` hazard is expected to dissolve, since the orchestrator
    awaits a whole cluster rather than an individual instantiation.

### 5.6 Registration, distribution, configuration

- `RegisterLibraryFromRequirementSpecifierRequest` (library_manager.py:2710) becomes the primary
    path, rewritten: validate the specifier, append to config, resolve and install, load via entry
    point. Its result carries a staged-restart flag when the resolve requires changing installed
    dists. Companion unregister and update requests follow the same shape, where update means
    re-resolve rather than git pull.
- `LibraryRegistration` (settings.py:125) carries a `specifier`, keeps `enabled`, and drops
    `worker_mode_override`. Config lookups previously keyed on registered path key on specifier.
- Editable development uses `-e /path/to/lib`. Code changes still require a restart once modules are
    imported, which is today's truth stated honestly; the reload flow re-runs resolution first so
    pyproject edits are picked up.
- `libraries_to_download` and its git provisioning are deprecated, since a `git+https` specifier
    expresses the same thing.
- GUI surfaces: register by specifier, update as re-resolve, version and duplicate display from
    installed dist metadata, an execution-unavailable state for venues not yet up, and cluster
    visualization per section 3.5.

### 5.7 Lifecycle path

One path, driven by the manifest: discover registered specifiers, resolve and install the base
environment, import each library's manifest via entry point, eagerly import node modules, harvest
decorated classes, register. Fitness evaluation, license checkpoints, and typed problems are
retained at their existing points.

The worker-delegated branch (`WORKER_DELEGATED`, `WORKER_PENDING`) is deleted along with stub
registration, because no library is ever loaded as a stub. Venue readiness is execution
availability, not load state.

### 5.8 Legacy libraries: fenced, not integrated

Executing as one piece allows a cleaner shim than the phased design proposed. Legacy JSON libraries
keep **today's behavior entirely**: their own venv, the sys.path splice, in-process execution, node
metadata parsed from JSON, and per-file module loading via `_load_module_from_file`
(library_manager.py:3428). Their dependencies do **not** enter the new resolution, and they get no
venue.

A legacy library has no base and exec split, so integrating its deps would either drag torch-class
pins into the clean environment or brick the library at registration. Fencing avoids both and keeps
the compatibility promise simple.

The honest cost: a fenced library still splices onto the orchestrator's `sys.path` and can shadow
packages in-process, which is precisely today's failure mode. Not a regression, bounded to the
migration window, and the reason the window is time-boxed.

- The adapter accepts both manifest filename spellings, `griptape_nodes_library.json` and
    `griptape-nodes-library.json`, as the discovery glob already does (library_manager.py:424-425).
- A deprecation problem attaches from the first release.
- Removal targets roughly two minor versions after first-party libraries publish package forms.
- The **sandbox library carve-out is permanent**: the file-backed loading path survives for it
    indefinitely, minus JSON dependency handling.

### 5.9 Workflow file compatibility

- Workflows reference nodes as `(library, node_type)` through `CreateNodeRequest`, so they are
    unaffected by packaging.
- Saved workflows embed parameter values as pickle with module paths patched into the stable
    namespace. `StableNamespaceImportFinder` (library_manager.py:305) is retained, and a packaged
    library may declare `legacy_module_aliases` mapping old stable-namespace paths to real modules so
    old saves unpickle. The finder and alias table outlive the shim by at least one release, timed
    against saved-file longevity.
- Eliminating pickle at rest is a separate effort. This design requires only that no pickle crosses
    a venue boundary.

## 6. Deletions

All in one pass, because nothing needs to coexist with a half-migrated world.

**Packaging**: `griptape_nodes_library.json` as an engine input, `LibrarySchema` as a parsed
artifact, the `Dependencies` model (library_registry.py:64), per-library Makefile `deps/sync`
targets, per-library venv creation (`_init_library_venv`, :2784), the sys.path splice
(`_add_library_paths_to_sys_path`, :2965) and `InstallLibraryDependenciesRequest` for everything but
the fenced legacy path, `LibraryDependencyDeclaration` git-URL resolution, and
`libraries_to_download` provisioning.

**Stub and schema machinery**: `_make_worker_stub_class` (:4451),
`_register_nodes_from_worker_schemas` (:4401), `_serialize_library_node_schemas` (:5032), the probe
timeout (`_SCHEMA_PROBE_TIMEOUT_S`, :4926), `WorkerNodeSchema` and `WorkerParameterSchema` and the
`LibraryLoadedNotification.node_schemas` field they type (app_events.py:267, a GUI-relayed payload
change), the `WORKER_DELEGATED` and `WORKER_PENDING` states, and the fitness problems that exist
only to describe stub limitations including the issue #4748 pair.

**Per-node execution**: `ExecuteNodeRequest`'s worker path, `_execute_node_via_worker`
(node_manager.py:3226), `_materialize_transient_node_from_metadata` (:3174),
`CancelExecuteNodeRequest`, the `FORWARDED_REQUEST_TYPES` allowlist and per-type `RemoteHandler`
swap (app/worker_routing.py:105, :239, :287), and ad-hoc artifact hydration across the RPC boundary
(`common/parameter_hydration.py`), which the boundary codec contract supersedes and which closes
issue #4475.

**Strict mode, entirely**: `common/strict_mode.py` (336 lines), `common/strict_mode_checks.py` (198
lines), the reporter singleton, both scope kinds, the severity resolver, the disable environment
variable, and violation plumbing in result details. Every rule describes a hazard this architecture
makes inexpressible: stub lossiness (venues run real nodes), parameter mutation failing to sync
(mutations are authoritative requests), worker reach into the orchestrator (now the sanctioned
mechanism), inert hooks (hooks run on real objects), and reentrancy in `__init__` (cluster dispatch
removes the blocking window).

**Parallel isolation path**: `SubprocessWorkflowExecutor`, its subprocess script and websocket
listener, and `workflow_packager.collect_dependencies`' JSON-deps merge (workflow_packager.py:455-475).
Export packaging survives but consumes specifiers and extras.

**Placement configuration**: everything in section 4.4.

## 7. Breaking changes and migration

### For library and node authors

1. The JSON manifest is gone: manifest class plus node decorators.
1. The library must be an installable package: build system, widgets and workflows as package data.
1. Base and exec split: heavy imports move behind the exec-module boundary. The substantive work for
    torch-class libraries, and note that **artifact modules are usually the sharp edge**, since types
    referenced by node modules and boundary codecs must be base-importable.
1. **Editor-time behavior must work on base deps.** Reflection over heavy classes has to become
    generated or declared data in the base package.
1. Imports that resolved only via the `sys.path` splice must become real package imports.
1. `pip_install_flags` becomes the static resolve-args table; library-repo `[tool.uv.*]` is dev-only.
1. Engine compatibility is a dependency pin, not metadata.
1. Library-to-library dependencies are ordinary package dependencies.
1. Versioning is pyproject plus tag.
1. `AdvancedNodeLibrary` becomes the manifest class, mechanically, with the same hook names.

**`process()` bodies do not change.** That is the payoff of rejecting purity: `self` works, events
work, streaming works, and mutating your own parameter set during execution starts working correctly
rather than silently failing to sync. The 242 `process`/`aprocess` bodies in the standard library
need no rewrite; what changes is which environment they run in and that manager access is forwarded.

Two patterns need attention, both currently worked around. Dynamic parameter shape recomputed from
input values (as in `vae_decoder._update_output_parameter`) routes through
`RemoveParameterFromNodeRequest` only because the local API does not cascade connection removal, an
acknowledged workaround with issue #2511 filed; fixing that makes it an ordinary local call.
Separately, hydration-driven value sets must be distinguishable from user-driven ones, or hydrating
inputs in a venue re-fires these hooks and echoes mutations the orchestrator already computed.
`SuccessFailureNode` reading its own outgoing connections to decide whether to re-raise is framework
policy expressed as a graph query, and the framework should decide with the node returning a
structured failure result.

### For users

Slower first boot after an engine update, because the environment is rebuilt. Dependency conflicts
surface as loud named errors at registration rather than mysterious runtime behavior. Libraries are
installed by specifier rather than cloned. Clusters are visible in the editor.

### For operators

The desktop app becomes an environment manager rather than shipping a frozen bundle, which changes
engine self-update semantics. Headless deployments gain the re-exec restart path.

## 8. Remote engines: a preserved future mode

Same-machine is the initial deployment target. It is not an architectural assumption, and the design
should not foreclose dispatching a cluster to a separately deployed engine.

**Version-locking orchestrator and venues is accepted policy**, including for future remote
deployments. That removes the obstacle that would have been most expensive to retrofit, since
forwarding the whole retained-mode surface makes the wire contract effectively the engine's own
surface.

**Already location-agnostic**: cluster computation, cluster dispatch, boundary codecs,
resolution-state residency with missing-value recovery, and statelessness itself. Statelessness in
particular *helps*, because a venue that forwards config and secret reads needs no local `.env` or
config file, which was the hardest problem in the earlier phased design.

**Required before remote works**:

- **Latency mitigations become mandatory rather than optional.** Read hydrated inputs locally, make
    progress fire-and-forget rather than awaited, and cache immutable reads for an execution's
    duration. Diffusers alone performs 22 config reads plus a progress emission per denoise step,
    which is noise at 1ms and seconds of overhead at 20 to 50ms. None of these changes the contract,
    only transport behavior.
- **Trust.** A venue the orchestrator did not spawn must authenticate, since provenance no longer
    establishes it. Secret values also cross a network, requiring transport security.
- **Partition handling.** Timeouts, idempotent retries for mutations, and a defined outcome when the
    link drops while the venue awaits a forwarded response.
- **Storage.** Either the presigned-URL driver, which already exists alongside the local driver, or a
    shared filesystem.

**Shared-filesystem remote is the likely first mode**, since facility infrastructure normally
provides it, and it should be expressed as driver configuration rather than an assumption in node
code. Two cautions. Network filesystems give close-to-open consistency and unreliable locking, and
this codebase has already been bitten by lock ordering, where `portalocker` creating a file before
acquiring its lock left zero-byte litter that poisoned every subsequent candidate. A local-looking
API also hides network throughput, so streaming tens of gigabytes of model weights over NFS is slow
in a way the code cannot see. Note that sharing the **model cache** is much lower risk than sharing
**output storage**: model files are write-once, read-many, and read only by venues, whereas outputs
are written by a venue and immediately read by the orchestrator, which is exactly where weak
consistency bites.

**Three constraints observed now, at no cost, to keep the option open**:

1. All file access goes through the storage driver abstraction, never raw shared paths.
1. Forwarding stays behind the transport-agnostic seam that `WorkerManager` already provides via
    `attach_transport`, rather than assuming a local socket.
1. Dispatch payloads are self-contained, with no implicit "read this from a path we both see" inputs.

Authentication, partition handling, and the latency mitigations are deliberately **not** built now.
None is foreclosed.

## 9. Implementation order

One release, internally ordered by dependency. Items on the same line are parallelizable.

1. **De-risk first, independent of everything else.** Reentrancy: generalize
    `worker_node_execution_scope` into a tested service-while-awaiting model against today's workers,
    and measure it. Issue #2511 connection cascade. Hydration-versus-user value-set distinction. All
    three are useful alone and all three are prerequisites in disguise.
1. **Authoring API.** Manifest class, node decorator, module walk, entry-point discovery, model
    surgery on `library_registry.py`. Fixture-package tests.
1. **Environment manager** and **exec-module convention with template CI lints**, in parallel.
1. **Lifecycle rewrite** onto the manifest path, plus the **legacy fence** and MIGRATION.md.
1. **Cluster computation** (union-find, extras resolution check, editor visualization payload).
1. **Venue runtime**: managers stripped except storage, catch-all forwarding proxy, resolution
    machine over a cluster. **Dispatch protocol and venue lifecycle** including fingerprints,
    resolution-state handling, and missing-value recovery, in parallel.
1. **Boundary codecs** and intra-cluster local value semantics.
1. **Registration, config, and specifier flows**; **desktop env manager**; **GUI workstream**
    including cluster visualization. All parallelizable.
1. **First-party migration**: template first as the scaffold, standard library second as the
    mechanical case, diffusers last as the proof point exercising the exec split, the generated
    descriptor table, venue execution, live latents across stages, and dynamic parameters together.
1. **Demolition** (section 6), once diffusers runs in a venue.
1. **Shim removal**, adoption-gated, trailing the release.

## 10. Verification

A single cutover cannot lean on incremental exposure, so verification is explicit.

- **Collision proof**: two libraries with historically incompatible pins register and load together;
    extras that cannot co-resolve within one cluster produce an error naming both libraries and the
    edge; an unsatisfiable base pin fails at registration with the culprit named while everything else
    loads.
- **Base-only proof**: the orchestrator environment contains no torch; every node of every migrated
    library instantiates, displays, serializes, and round-trips through save and load with no venue
    running; editor-time dynamic parameters work with no venue running.
- **Cluster proof**: union-find produces the expected clusters for each shipped diffusers template
    with no authoring changes; a user-drawn boundary cutting an unserializable edge is rejected with a
    clear message.
- **Latent proof**: a multi-node diffusers cluster runs in a venue passing live tensors in-process,
    with progress streaming to the GUI and outputs landing on the orchestrator.
- **Residency proof**: a multi-stage template hands a latent between dispatches; re-running with a
    changed seed reuses the resident pipeline without reloading it; reaping the venue mid-workflow
    triggers missing-value recovery and re-execution rather than a wrong result.
- **Boundary proof**: a non-serializable value on a cluster boundary fails at dispatch naming the
    parameter, rather than at runtime.
- **Forwarding proof**: a node performing config, secret, static-file, and graph-mutation access
    during execution succeeds; the same access is screened by license policy; reentrant service during
    an in-flight dispatch is exercised under contention with measured latency and no deadlock.
- **Legacy proof**: an unmigrated JSON library, including a heavy one, loads and executes exactly as
    before, with its deps absent from the new resolution.
- **Migration proof**: old saved workflows open, including ones with pickled library-defined values,
    degrading gracefully where a value's class is behind the exec boundary.
- **Platform**: the whole matrix on Windows, with attention to long paths, file locks during staged
    environment swap, and CUDA index resolution.

## 11. Risks

1. **Reentrancy becomes the hot path.** Forwarding everything means the orchestrator services
    forwarded requests while awaiting its own dispatch, on machinery that already stalls under
    cross-loop contention. Mitigated by making it the first work item, measured before anything
    depends on it.
1. **Unconditional forwarding latency.** Every config read, secret read, and boundary input re-read
    is a round trip; hot spots are per-token streaming (18 files, Agent streams per token) and loops
    re-reading parameter values. Same-machine IPC makes this far cheaper than a network hop. Accepted,
    with mitigations available that do not change the contract, and which become mandatory if remote
    is ever pursued (section 8).
1. **Single cutover concentrates risk.** No intermediate release delivers partial value, so a slip in
    any component slips everything.
1. **VRAM pressure from long-lived venues.** Intermediate values sit on node instances until
    recomputed, which is today's behavior, but concurrent runs across different workflows multiply
    resident memory. Needs an admission cap or per-venue serialization, with idle reaping as the
    release valve.
1. **Desktop env-manager work is on the critical path.** Seed wheels, bundled installer, first-boot
    environment build, changed self-update semantics. The overlay venv is the documented fallback.
1. **One-resolution strictness is a UX cliff.** Conflicts that used to shadow silently now block
    registration, so bisection and problem reporting have to be genuinely good and docs must teach the
    execution extra as the pressure valve.
1. **Windows.** Long paths, file locks against the staged swap, CUDA index behavior.
1. **Fenced legacy libraries can still shadow packages in-process**, exactly as today, for the
    migration window.
1. **Cluster granularity can surprise users.** A single unserializable edge silently merges two
    regions into one venue, which is correct but can produce a larger dispatch unit than expected.
    Editor visualization is the mitigation.
1. **Foreign graph reads race with parallel resolution.** Permitted by decision; reads are
    authoritative but arbitrary in time. The well-behaved pattern remains wiring a connection so the
    scheduler orders the dependency.
1. **Unbounded reach keeps libraries coupled to engine internals**, so libraries stay
    version-brittle and third-party libraries are unattenuated during execution beyond what license
    policy screens. Version-locking is accepted policy, which makes this a maintenance cost rather
    than a compatibility hazard.
1. **Entry-point discovery executes third-party code at startup**, the same trust boundary as
    today's node import at an earlier moment.
1. **uv as a runtime dependency deepens** (engine issue #833): bundled binary versus wheel
    dependency, decide explicitly.

## 12. Open questions

1. **Per-project registration and pins versus one environment.** Projects carry their own
    registrations and version pins, which per-project roots isolate today. One environment cannot hold
    two projects' conflicting pins, and naive per-project keying forces a rebuild and restart on every
    project switch. The recommendation on the table is a union environment where cross-project
    conflicts are loud and named and switching never rebuilds.
1. **Variable resolution under forwarding.** `aprocess_scope(precomputed_variables)`
    (node_types.py:135) exists so an executing node needs no registry access to resolve substitutions.
    Under forward-everything it is arguably redundant but also cheaper. Decide whether it stays
    pre-seeded or becomes another forwarded read.
1. **Venue consolidation heuristics.** Whether to merge compatible extras sets into fewer venues to
    save memory, or keep them separate for isolation and simpler reaping.
1. **Cluster visualization design.** The mechanism produces the grouping; how it reads on the canvas
    (tint, outline, badge, or an explicit overlay mode) is undesigned.
1. **Concurrent run admission.** Whether venues serialize runs, cap concurrent sessions, or defer to
    an orchestrator-level scheduler.
1. **Pickle at rest** is owned by a separate effort; this design requires only a pickle-free venue
    boundary.
