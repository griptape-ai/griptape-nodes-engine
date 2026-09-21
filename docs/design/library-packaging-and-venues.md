# Libraries as Python Packages + Subflow Execution Venues

Status: proposal for team review. No code accompanies this document.

This design converts Griptape Nodes libraries from JSON-manifest folders into normal Python
packages, replaces N independent dependency resolutions with one, and (in a second phase)
moves all isolated node execution to venue engines that receive whole subflows. Phase 1 is
a packaging change that ships alone; execution topology does not move until phase 2.

## 1. Motivation and decisions

Library dependencies today are broken by design:

- Deps are declared in `griptape_nodes_library.json` and duplicated in `pyproject.toml`.
    The only sync is each library's Makefile `deps/sync` target (toml to json); nothing
    verifies it, and the engine never reads a library's pyproject.
- Per-library venvs exist, but every Shared library's site-packages is spliced onto the one
    orchestrator `sys.path` (`_add_library_paths_to_sys_path`, library_manager.py:2965).
    That splice is the real collision surface: the last-loaded library's copy of a package
    silently shadows everyone else's.
- Worker mode (Isolated) avoids the splice, but at the cost of lossy stub classes on the
    orchestrator (no converters, validators, traits, or hooks), per-node RPC, and a strict-mode
    rule set that exists mostly to describe what the stubs lose.

### Decisions (locked)

| Topic              | Decision                                                                                                                                                                                                                                                                                                                                                       |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dependency model   | (End state) "Orchestrator installs base packages (GUI, instantiation, graph editing - all real nodes, no stubs). Each venue installs base + [exec] for the libraries whose subflows it hosts." Phase 1 transitionally deviates: in-proc heavy libraries' [exec] installs into the orchestrator env until subflow dispatch exists (section 7).                  |
| Manifest           | `griptape_nodes_library.json` dies; pyproject.toml is package-level truth (deps, extras, discovery entry point, static `[tool.griptape-nodes]` resolve-args table). Node metadata stays in code.                                                                                                                                                               |
| Node metadata      | Code-derived: decorators/introspection on the base package. Precedents: pytest plugins, Django AppConfig, Airflow providers. Decorators annotate only; registration is pull-based. Directory-scale listing of uninstalled libraries is served by a build-time derived-metadata artifact, never hand-written JSON.                                              |
| Extras granularity | One execution extra per library, convention name `[exec]`.                                                                                                                                                                                                                                                                                                     |
| Execution venue    | A venue is a griptape engine, period (the -app binary; license validation is native to every venue). Local venues are auto-spawned (the worker successor); remote venues are user-managed distributed engines.                                                                                                                                                 |
| Dispatch unit      | The subflow is the unit; per-node `ExecuteNodeRequest` dispatch is deleted in the end state. Live values (latents) flow inside the subflow; only boundary values serialize.                                                                                                                                                                                    |
| Subflow UX         | Deferred (auto-bundling adjacent heavy nodes vs. documentation-only is not decided here).                                                                                                                                                                                                                                                                      |
| Venue lifetime     | Policy-driven: default long-lived per library; metadata may request ephemeral or pinned.                                                                                                                                                                                                                                                                       |
| Orchestrator env   | One resolved env: uv resolves engine + all base packages together; conflicts fail loudly at registration with the culprit named.                                                                                                                                                                                                                               |
| Exec imports       | Exec-module convention: node modules are base-clean; heavy logic lives in sibling `exec/` modules with normal top-of-file imports; `_process` lazily imports and delegates. CI-lintable.                                                                                                                                                                       |
| Phase-1 heavy exec | Execution topology unchanged in phase 1. Heavy libraries that cannot run isolated (diffusers: strict-mode violations, untransportable latents) keep running in-proc; the orchestrator installs their `[exec]` into the resolved env (transitional, per-library, loud conflicts). "Orchestrator base-only" is the phase-2 end state, not a phase-1 requirement. |
| Distribution       | Any requirement specifier (PyPI, git+https, local/editable). `RegisterLibraryFromRequirementSpecifierRequest` (library_manager.py:2710) is the seed.                                                                                                                                                                                                           |
| Compat shim        | Synthesize packages from legacy JSON at load (manifest-level adapter) so one loader exists; time-boxed.                                                                                                                                                                                                                                                        |
| Engine compat      | The engine is a normal base dep (`griptape-nodes-engine>=X,<Y`); the single resolution enforces it; a template lint guards pin width.                                                                                                                                                                                                                          |
| Sequencing         | Packaging first (phase 1 shippable alone); execution redesign in later phases.                                                                                                                                                                                                                                                                                 |
| Env implementation | Full replacement venv now (not an overlay): one venv containing engine + app + all base packages, resolved together; the engine process spawns from it. The desktop app becomes an env manager; that work lives in griptape-nodes-desktop (see section 4).                                                                                                     |
| Import posture     | Eager: the loader imports every non-exec module at library load and harvests decorated classes; authoring errors surface at load.                                                                                                                                                                                                                              |
| Pickle             | Owned by a parallel effort (kill-pickle-everywhere). This design only specifies the venue wire as pickle-free and cross-references that work for saves.                                                                                                                                                                                                        |

## 2. Current state

File/line anchors are against the engine tree at the time of writing.

- **Library lifecycle**: `LibraryManager._progress_library_through_lifecycle`
    (library_manager.py:2241) drives DISCOVERED -> METADATA_LOADED -> EVALUATED ->
    DEPENDENCIES_INSTALLED -> LOADED, with a worker branch EVALUATED -> WORKER_DELEGATED ->
    WORKER_PENDING -> LOADED. Roughly 40 typed fitness-problem classes attach per phase;
    license/authorization checkpoints run at EVALUATED.
- **Dependency installation**: per-library venv created with uv (`_init_library_venv`,
    library_manager.py:2784), deps installed via `uv pip install ... --python <venv>`
    (install handler around :6777), then the venv's site-packages and the library dir are
    spliced onto the orchestrator's `sys.path` (`_add_library_paths_to_sys_path`, :2965).
    Discovery accepts both manifest spellings via the glob at :424-425.
- **Node loading**: node metadata comes entirely from the JSON, never from imports. Modules
    load via `importlib.util.spec_from_file_location` with hashed dynamic names plus a stable
    alias namespace (`_load_module_from_file` :3428; `StableNamespaceImportFinder` :305).
    Lazy registration (`NodeTypeEntry`, library_registry.py:536) defers module import to first
    node creation.
- **Worker mode (WorkerV1)**: a per-library, full-engine subprocess spawned as
    `sys.executable -m griptape_nodes_app engine --session-id X --library-name Y`
    (worker_manager.py:612), with exact engine-version lockstep enforced at registration.
    The orchestrator skips venv/dep-install/node-imports for worker libraries
    (library_manager.py:2443, :2491, :2542) and registers synthesized stub classes built from
    worker-probed schemas (`_make_worker_stub_class` :4451, `_register_nodes_from_worker_schemas`
    :4401, `_serialize_library_node_schemas` :5032). Execution is per-node RPC:
    `ExecuteNodeRequest` materializes a fresh transient node per call on the worker
    (node_manager.py:3174), which is stateless between calls (:3111); outputs round-trip
    through the orchestrator as JSON. About 40 request types forward from worker to
    orchestrator through `RemoteHandler` shims (`FORWARDED_REQUEST_TYPES`, app repo
    worker_routing.py). `get_worker_for_library` (library_manager.py:768-792) raises when a
    worker library has no live worker; node_manager.py:3148 calls it unguarded.
- **Existing subflow isolation**: `SubflowNodeGroup` carries an `execution_environment`
    parameter (LOCAL_EXECUTION / PRIVATE_EXECUTION / a library name) dispatched in
    common/node_executor.py:250-264. PRIVATE_EXECUTION packages the subflow as a standalone
    workflow (`workflow_packager.collect_dependencies`, workflow_packager.py:455-475, which
    merges libraries' JSON `pip_dependencies`) and runs it under `SubprocessWorkflowExecutor`
    with events relayed back.
- **Desktop packaging**: the desktop app ships the engine, its interpreter, and all deps
    frozen inside the app bundle; nothing is installed at runtime and uv is dev-only
    (griptape-nodes-desktop `src/common/services/gtn/engine-service.ts:304`, `:318`;
    `gtn-service.ts:726`).

## 3. Library package contract

A library is a normal, installable Python package.

```toml
[project]
name = "griptape-nodes-library-example"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = [
  # BASE deps: enough to import every node module and instantiate every node.
  "griptape-nodes-engine>=0.99,<0.110",
  "pillow>=11.2.1",
]

[project.optional-dependencies]
# One execution extra, by convention named "exec".
exec = ["torch==2.7.0", "diffusers==0.39.0"]

[project.entry-points."griptape_nodes.library"]
example = "example_library.library:LIBRARY"
```

- **Layout**: `mylib/library.py` (manifest), `mylib/nodes/*.py` (base-clean node classes),
    `mylib/exec/*.py` (heavy logic with normal top-of-file imports). `_process` performs one
    sanctioned lazy import of the exec module and delegates.
- **Index/backend channel (replaces `pip_install_flags`)**: `[tool.uv.sources]` and
    `[[tool.uv.index]]` are uv project config, honored only by `uv sync` in the library repo,
    never when the package installs as a dependency via `uv pip install` (which is every
    install path in this design). Index/backend requirements (CUDA torch index,
    `--torch-backend`) therefore become declared data the `LibraryEnvironmentManager` reads
    pre-resolve and merges resolve-wide, warning loudly on cross-library conflicts. Carrier:
    a static `[tool.griptape-nodes]` table in pyproject.toml, tomllib-readable pre-install
    from an sdist, git checkout, or editable path. Wheels do not contain pyproject.toml (only
    package files plus `.dist-info/`), so for wheel-distributed libraries the authoritative
    pre-resolve read path is the build-time derived-metadata artifact, which lives inside the
    wheel and mirrors the table at build time. A per-registration config override exists as
    an escape hatch. Library-repo `[tool.uv.*]` remains for dev `uv sync` only. Phase-2 venue
    installs consume the identical channel.
- **CI lints (shipped in the template)**: (a) clean-venv base-only install plus an
    import-walk of every non-exec module; (b) a script diffing non-exec imports against base
    deps; (c) an engine-pin width check.

### Authoring API (replaces the JSON as the source of LibrarySchema)

The existing pydantic models (`LibraryMetadata`, `NodeMetadata`, `CategoryDefinition`,
`Setting`, `WidgetDefinition`, declarations) survive; only their source changes.

- `@node_metadata(category=..., display_name=..., icon=..., deprecation=..., declarations=[...])` validates through `NodeMetadata` at import and stores the result as
    `__gtn_node_metadata__` on the class. Annotate-only, no registry side effects (the
    ComfyUI lesson); the engine harvests pull-based.
- A `LibraryManifest` class absorbs `AdvancedNodeLibrary`: name, metadata, categories,
    settings, widgets, `get_node_modules()` (default: walk the package excluding `exec/`),
    plus the existing hooks (`before/after_library_nodes_loaded`, `get_request_handlers`,
    `get_post_dispatch_hooks`). Exported as `LIBRARY` via the entry point.
- `library_version` and `engine_version` leave metadata: version comes from
    `importlib.metadata`, engine compatibility from the dependency pin. The `Dependencies`
    model is deleted. Widgets and workflows become package data (`importlib.resources`); the
    wheel build must include them.
- Import posture is eager: base modules are cheap by construction, and eager import kills
    the lazy-entry footguns (deferred import failures at first canvas drop). `NodeTypeEntry`
    remains as the internal registry type.
- **Base-clean is a contract on every orchestrator-side surface**, not just node modules:
    manifest hooks, `get_request_handlers()` / `get_post_dispatch_hooks()` (which flip from
    unreachable-with-warning on worker libraries today, issue #4748, to running locally
    base-only in phase 1), converters/validators/traits, and anything reachable from a pickled
    save. The rule: anything that can execute on the orchestrator or appear in a saved
    workflow must be importable and functional with base deps only. Old saves that violate it
    degrade gracefully: the value drops to default with a load warning and the node is marked
    unresolved, rather than failing the whole workflow open.

## 4. Orchestrator resolved environment

A new `LibraryEnvironmentManager` owns resolve/build/staged-rebuild. Three options were
analyzed; Option B is the design.

- **Option B (the design): full replacement venv.** One venv containing engine + app + all
    libraries' base packages (plus, during phase 1, the transitional `[exec]` extras of
    in-proc heavy libraries per section 7), resolved literally together; the engine process
    itself spawns from it. This is the truest single-resolution story with no overlay-skew
    risk. The cost pulled into phase 1: the desktop app becomes an env manager. The bundle
    ships seed wheels plus a bundled uv; first boot (and engine updates) build the env; engine
    self-update semantics change. This is griptape-nodes-desktop work (engine-service.ts /
    gtn-service.ts, which today spawn the immutable bundled engine), not griptape-nodes-app,
    which is the Python/Rust engine package that venues spawn from.
- Option A (constraint-locked overlay venv in user data; no desktop packaging change) was
    considered and rejected as the design; it is documented as the fallback if the desktop
    env-manager work slips phase-1 timing.
- Option C (uv project at the workspace root) is rejected: workspaces are art projects, not
    Python projects.

Mechanics:

- **Apply model**: hot-install for pure additions; staged rebuild plus restart when any
    installed dist must change (Python cannot un-import; see the existing
    `_libraries_reloaded_after_import` scar tissue). Artifacts beside the env:
    `requirements.in`, `engine-constraints.txt`, `resolution.lock`.
- **Failure UX**: on a failed resolve, bisect (constraints plus each library alone, then
    pairs) and emit a typed
    `DependencyResolutionConflictProblem(library, requirement, conflicts_with, uv_stderr)`.
    The offending library goes FAILURE/UNUSABLE and the rest are re-resolved; one bad library
    never blocks startup.
- **Keying**: env keyed by engine + python version; an engine upgrade rebuilds from
    `requirements.in` on first boot, with per-library failures isolated. Disk-space and
    writability checks carry over from the current venv code.
- **File-level collision guard**: two dists shipping the same top-level import package into
    one venv is last-write-wins for uv - silent shadowing, the same bug class this design
    kills at the dist level. Add a registration-time overlap check across installed dists'
    RECORDs plus a template lint requiring a unique, library-named top-level package.
- **Capability ownership**: `LibraryEnvironmentManager` lives engine-side; phase-2 venues
    and headless/docker/CLI engines need the identical capability. The desktop app is one
    packaging/integration of it, never the owner.
- **Restart choreography**: the engine spawns from the venv being replaced, so something
    must restart it into the staged env. Desktop: the app respawns the engine (existing
    lifecycle). Headless/CLI/docker: the engine re-execs itself into the staged env's
    interpreter (`os.execv`) by default, with operator-restart as the documented fallback
    where re-exec is unsafe (interacts with the Windows file-lock risk in section 11).
- **Per-project registration and pins (OPEN policy question)**: projects carry their own
    library registrations and PEP 440 pins today (per-project `libraries_dir`,
    `_reconcile_libraries_from_config` on activation, settings.py:189-191), which per-project
    roots isolate. One env cannot hold two projects' conflicting pins, and naive per-project
    env keying would force a rebuild plus restart on every project switch. Options:
    (a) union env - all projects' registrations co-resolve, cross-project conflicts are loud
    and named, project switch never rebuilds (recommended: conflicting pins become an explicit
    error to align, vs. today's silent divergence); (b) per-project env keying (isolation
    preserved, brutal switch UX); (c) global-only registration with per-project pins demoted
    to constraints.

## 5. Loading and registration

- **Discovery**: installed libraries are found via the `griptape_nodes.library` entry-point
    group. Loading imports the manifest module, then eagerly imports the modules from
    `get_node_modules()` and harvests `__gtn_node_metadata__`-annotated `BaseNode` subclasses
    into the registry.
- **Registration**: `RegisterLibraryFromRequirementSpecifierRequest` is rewritten as the
    primary path: validate the specifier, append to config,
    `LibraryEnvironmentManager.resolve_and_install()`, load via entry point. The result
    carries a staged-restart flag when the resolve requires changing installed dists.
    Companion Unregister/Update requests follow the same shape (update = re-resolve, not git
    tag).
- **Config**: `LibraryRegistration` (settings.py:125) gains `specifier` alongside the legacy
    `path` (exactly one set); `enabled` and `worker_mode_override` carry over. Note that
    `_resolve_requires_worker` matches config entries by path today
    (library_manager.py:5310-5358); the lookup rekeys by specifier for packaged libraries.
    Default config ships the standard library as a specifier.
- **Editable dev**: `-e /path/to/lib`. Code changes still require a restart once modules
    were imported (the same truth as today, now stated honestly); the reload flow re-runs
    resolution first so pyproject edits are picked up.
- **Directory-scale listing of uninstalled libraries**: a build-time derived-metadata
    artifact (CI imports the base package and dumps derived JSON into the wheel and the
    directory index). Derived, never hand-written; also the wheel-side carrier for the
    resolve-args table (section 3).
- **GUI/editor surfaces**: library management UI moves from path/git-centric to
    specifier-centric (register by specifier, update = re-resolve, duplicate/version
    display); see the work breakdown for the GUI workstream.

## 6. Legacy shim and migration timeline

Legacy JSON libraries are synthesized into manifests at load, so one loader exists.

- `node_library/legacy_manifest_adapter.py`: JSON -> a `LibraryManifest` instance backed by
    the existing per-file module loaders. For Shared/in-proc legacy libraries, JSON
    `pip_dependencies` enter the single resolution verbatim and `pip_install_flags` translate
    to resolve-wide uv args (loud warning on conflicts between legacy libraries). Legacy
    worker libraries' JSON deps stay worker-side (per-library worker venv, retained path) and
    never touch the orchestrator resolve. The adapter accepts both manifest filename
    spellings, `griptape_nodes_library.json` and `griptape-nodes-library.json`, as the
    discovery glob already does (library_manager.py:424-425).
- The lifecycle state machine gets one post-DISCOVERED path for loading - manifest (adapter
    or entry point) -> resolve env -> load nodes - with one explicitly surviving branch during
    the shim window: legacy worker libraries keep the WORKER_DELEGATED -> WORKER_PENDING ->
    stub path until the shim dies. The per-library `_init_library_venv` / splice /
    `InstallLibraryDependenciesRequest` machinery is deleted from the orchestrator path (the
    worker path retains it until phase 2; see section 7).
- **Legacy Shared heavy libraries**: their `pip_dependencies` entering the single resolution
    means torch-scale deps join the replacement venv, their `pip_install_flags` become
    resolve-wide, and a pin conflict with engine/app bricks the library (FAILURE) where
    today's splice let it limp. This trade is deliberate; the escape hatches are migrating to
    package form or `worker_mode_override` (stub-quality GUI).
- **Honest limitation**: legacy heavy libraries (no base/exec split) cannot give the
    orchestrator real nodes; they stay on stub + WorkerV1 until they migrate. Stub machinery
    deletes per-library as migration proceeds, and fully with the shim.
- **Time-box**: a deprecation fitness problem from day one; removal targeted roughly two
    minor versions after the first-party libraries publish package forms. The
    `libraries_to_download` git plumbing is deprecated with it (git+ specifiers replace it).
- **Sandbox carve-out**: the sandbox library stays file-based by design, permanently. The
    file-backed manifest adapter and per-file module loaders survive for sandbox only; the
    JSON pip-deps/venv/pip_install_flags handling does not. "One loader" means one lifecycle
    path consuming `LibraryManifest`, with two manifest sources (entry point; file adapter
    for sandbox and, during the window, legacy JSON).
- **Workflow-file compatibility**: workflows reference nodes by `(library, node_type)`
    through CreateNodeRequest and are unaffected. Pickled values referencing stable-namespace
    module paths keep working via `StableNamespaceImportFinder` (library_manager.py:305);
    packaged libraries may declare `legacy_module_aliases` (old stable namespace -> real
    module) so old saves unpickle. The finder and aliases die with (or one release after) the
    shim, given saved-file longevity.

## 7. Phase-1 execution posture (topology unchanged)

Phase 1 changes where deps come from, not where code runs.

- **In-proc libraries (including diffusers-class heavy)**: keep executing in the
    orchestrator exactly as today. The packaged form declares transitional in-proc execution;
    the orchestrator installs `<lib>[exec]` into the resolved env alongside base. Latent
    workflows are unaffected. Heavy pins join the single resolution, so conflicts become loud
    and named (vs. today's silent splice shadowing); removing heavy pins from the orchestrator
    resolve entirely is the phase-2 payoff. The exception list drains as subflow dispatch
    ships.
- **Worker (Isolated) libraries**: `requires_worker` no longer gates loading (the skip
    branches at library_manager.py:2443/2491/2542 are removed); it becomes a pure
    execution-routing flag consumed by `NodeManager.on_execute_node_request` (:3147). Real
    node classes exist in ObjectManager from the base import; per-node RPC to the WorkerV1
    worker is unchanged. Only libraries that are worker-compatible today (serializable edges,
    no strict violations) live here - the same population as today.
- **Routing hard rule (already exists; preserve it)**: `get_worker_for_library`
    (library_manager.py:768-792) already raises when `requires_worker` is set and no worker
    is registered, and node_manager.py:3148 calls it unguarded; the local fall-through at
    :3147-3151 is only reachable for non-worker libraries. Phase-1 work: (a) preserve that
    guard as `requires_worker` becomes a pure routing flag - it must keep raising, never fall
    through to local, since real base-only classes now exist locally with `[exec]` absent;
    (b) add the missing UX - worker eviction sets an "execution unavailable" status,
    optionally queue-until-ready during worker startup. Legacy stub libraries keep riding
    WORKER_PENDING until the shim dies.
- **Worker execution env**: the per-library venv + sys.path splice machinery is deleted from
    the orchestrator path in phase 1 but retained on the worker path only, time-boxed: the
    WorkerV1 worker (spawned from the replacement venv's interpreter) resolves the config
    specifier, installs `<specifier>[exec]` into a per-library venv constrained to the
    engine's pins, splices that venv onto its own sys.path, and loads via the normal package
    loader. This worker-side machinery dies in phase 2 with per-node RPC.
- **No second engine dist in the worker venv**: packaged libraries' base deps include the
    engine pin, so a naive `[exec]` install would pull a second engine copy into the
    per-library venv, front-splicing over the running engine for any module not yet imported.
    Failure modes: editable-dev divergence (the replacement venv runs `-e` repo code while
    the worker venv resolves the same version from PyPI) and seed-wheel bricking (the running
    version not published to the index, making an exact-pin constraint unsatisfiable). The
    worker-venv install must satisfy the engine requirement from the running environment:
    exclusion/override of the engine (and app) dists at install, or a post-install strip, so
    engine modules always come from the spawning interpreter.
- **Workflow packager is a phase-1 runtime dependency**: PRIVATE_EXECUTION and library-name
    subflow execution route through `_publish_local_workflow` ->
    `workflow_packager.collect_dependencies` (workflow_packager.py:455-475), which reads
    `metadata.dependencies.pip_dependencies` - the model phase 1 deletes for packaged
    libraries. Named work item: the packager consumes the library's requirement specifier
    plus `[exec]` for packaged libraries; the JSON-deps merge survives only for legacy
    libraries via the shim and dies with it.

### Delete now (packaged libraries; fully once the shim dies)

`_make_worker_stub_class`, `_register_nodes_from_worker_schemas`,
`_serialize_library_node_schemas`, `WorkerNodeSchema` (including the
`LibraryLoadedNotification.node_schemas` field it types - a GUI-relayed event payload
change, app_events.py:267), the schema-probe timeout machinery, the strict-mode rules
`connection-hooks-inert-on-worker`, `value-hooks-execute-only-on-worker`, and
`parameter-behaviors-dropped-in-schema`, and the WORKER_DELEGATED / WORKER_PENDING states
(worker startup gates "execution available", not "library loaded").

### Must wait for phase 2

The per-node RPC path, `FORWARDED_REQUEST_TYPES` / `RemoteHandler`, the
`worker-reach-into-orchestrator` rule, `parameter_hydration.py`, and the worker config/
secrets/project broadcasts.

Nothing regresses in phase 1 because nothing moves: in-proc stays in-proc (LatentArtifact
flows in the orchestrator as today; WorkerV1 is stateless per ExecuteNodeRequest and
round-trips values through the orchestrator as JSON, node_manager.py:3111, so worker
placement was never an option for latent chains), and isolated stays isolated.

## 8. Engine compatibility and upgrade flow

- Libraries declare `griptape-nodes-engine>=X,<Y` as a normal base dependency. The
    template ships a sensible window with a comment; a `check-engine-pin` CI lint fails on
    unbounded or exact (`==`) pins.
- Enforcement is free: the resolved env always contains the running engine version, so an
    out-of-window library fails the single resolution and gets named by the bisection. This
    replaces the string-compare `engine_version` metadata check.
- Engine upgrade: a new engine version means a new env key and a rebuild from
    `requirements.in` on first boot. Libraries that no longer resolve fail individually with
    a typed problem; everything else loads. An upgrade never bricks the whole node picker.
    The desktop updater gains a "first boot after update may take minutes (env rebuild)"
    progress surface, reusing the library-load status events.

## 9. Breaking changes and template/CI plan

For node authors, honestly:

1. The JSON manifest is deleted: `LibraryManifest` + `@node_metadata` replace it.
1. A library must be an installable package (build-system; widgets/workflows as package
    data).
1. Base/exec split: heavy imports move to `exec/` (the migration cost for diffusers-class
    libraries).
1. sys.path-splice relative imports (`from utils.foo import ...`) break; use real package
    imports.
1. `pip_install_flags` becomes static resolve-args/index data in pyproject's
    `[tool.griptape-nodes]` table (read pre-install from sdist/git/editable; from the
    in-wheel derived-metadata artifact for wheels). Library-repo `[tool.uv.*]` is dev-only.
1. Engine compatibility via the dependency pin, not `engine_version` metadata.
1. `LibraryDependencyDeclaration` git URLs become normal dependencies.
1. Versioning is pyproject + git tag; the Makefile version targets re-point.
1. `AdvancedNodeLibrary` becomes `LibraryManifest` (mechanical; same hook names).

Template repo updates: package scaffold, `library.py` manifest, `exec/` example, the three
CI lints from section 3, updated Makefile, README rewrite. Engine docs: libraries guide,
configuration reference, MIGRATION.md.

## 10. Phase 2: venues and subflow dispatch (directional)

Phase 2 is deliberately one level less specified than phase 1; it lands as its own design
iteration against a stable packaging base.

- **Anchor on what exists**: `SubflowNodeGroup.execution_environment` (node_executor.py:
    250-264) becomes a venue selector; PRIVATE_EXECUTION's packager +
    `SubprocessWorkflowExecutor` + event-relay machinery is the seed, promoted from anonymous
    subprocess to venue protocol.
- **Venue lifecycle**: reuse the WorkerManager spawn/register/heartbeat/evict/route
    machinery nearly verbatim (Worker -> Venue). Remote venues self-register on session
    topics. Keep the engine-version lockstep initially; relax to a compatible range once the
    boundary contract is versioned. A `VenueLifetime` declaration (LONG_LIVED | EPHEMERAL |
    PINNED, idle timeout) implements the policy-driven lifetime decision.
- **Protocol sketch**: `ExecuteSubflowRequest{subflow_spec, input_values, execution_id}` -
    the venue instantiates real nodes, runs its own resolution machine (live values stay
    in-process, which is what deletes per-node RPC), streams ExecutionEvents for GUI relay,
    and returns `ExecuteSubflowResult{output_values, errors}`. `CancelSubflowRequest`
    replaces `CancelExecuteNodeRequest`.
- **Boundary serialization v1**: JSON-safe scalars plus registered artifact codecs living
    in libraries' base packages so both sides can decode (resolves issue #4475). No pickle on
    the wire. A non-serializable value at a boundary is a dispatch-time validation error,
    keeping tensor edges inside one venue with a good message. Pickle-at-rest (workflow
    saves) is owned by the parallel kill-pickle effort; this design only specifies the wire.
- **Config/secrets to remote venues (open; three framed options)**: (1) scoped push per
    dispatch; (2) venue-local provisioning, failing with "venue missing secret X"
    (recommended default); (3) short-lived secret leases (cloud). Today's workers share the
    orchestrator's disk, which is exactly what breaks remotely.
- **End-state deletions**: the per-node RPC path and transient-node materialization, the
    worker forwarding shims, the remaining strict-mode worker rules,
    `SubprocessWorkflowExecutor`/subprocess_script (folded into the venue client), and the
    `workflow_packager.collect_dependencies` JSON-deps merge.

## 11. Risks and open questions

1. Option B pulls a desktop packaging overhaul into phase 1 (app as env manager: bundled uv
    - seed wheels, first-boot env build, engine self-update semantics). Biggest schedule
        risk of the phase; Option A (overlay) is the documented fallback if it slips.
1. Windows: long paths, file locks vs. the staged-env swap, CUDA index behavior in a single
    resolution. Needs an explicit test matrix.
1. Legacy heavy libraries keep stub-quality GUI until they migrate; communicate this so it
    does not read as a regression of the new architecture.
1. One-resolution strictness is a UX cliff vs. today's "works until it doesn't". The
    bisection/problem UX must be excellent, and docs must teach `[exec]` as the pressure
    valve. During phase 1, in-proc heavy libraries' `[exec]` pins do join the orchestrator
    resolve (transitional, loud); only phase 2 removes them entirely.
1. Entry-point discovery means third-party code executes at engine startup - the same trust
    boundary as today's node import, at an earlier moment. The app permission/license layer
    is the enforcement home.
1. uv as a runtime dependency deepens (engine TODO #833): bundled binary vs. wheel dep,
    decide explicitly.
1. Per-project registration/pins vs. the one env (section 4) needs a policy decision.
1. Phase-2 remote security (venue authentication, secrets distribution) is genuinely open.

## 12. Work breakdown and sequencing (phase 1, component level)

1. **Authoring API**: `node_library/manifest.py` (`LibraryManifest`, `@node_metadata`,
    default module walk), model surgery on library_registry.py, entry-point discovery, unit
    tests against a fixture package.
1. **LibraryEnvironmentManager**: replacement-venv lifecycle, single resolve+install,
    staging/restart handshake, bisection + `DependencyResolutionConflictProblem`,
    lock/requirements artifacts, file-level collision guard.
1. **Lifecycle rewrite**: `_progress_library_through_lifecycle` converges on
    manifest-driven loading; widget/settings from package data; the orchestrator-side
    venv/splice path is deleted.
1. **Legacy adapter + MIGRATION.md**: JSON-to-manifest adapter (both filename spellings),
    requirement contribution scoped by shared vs. worker, stable-namespace alias table,
    deprecation problem, sandbox carve-out.
1. **Registration/config**: `LibraryRegistration.specifier`, rewritten specifier request
    family, `worker_mode_override` rekeying, editable dev flow, config migration.
1. **WorkerV1 coexistence**: worker-side `[exec]` install (engine dist excluded),
    real-node loading for worker libraries, stub/schema-probe deletion, strict-rule
    retirement, "execution unavailable" status.
1. **Workflow packager**: consumes specifier + `[exec]` for packaged libraries
    (PRIVATE_EXECUTION / library-name execution runtime path).
1. **First-party library migration**: standard (mostly mechanical), diffusers (the
    exec-split proof point that drives the convention's ergonomics), template (scaffold +
    CI lints).
1. **Desktop app as env manager** (griptape-nodes-desktop): bundled uv + seed wheels,
    first-boot env build, engine self-update semantics, rebuild progress UX.
1. **GUI editor workstream** (griptape-vsl-gui): register-by-specifier UI, update =
    re-resolve, duplicate/version display, `LibraryLoadedNotification.node_schemas` payload
    change, "execution unavailable" status.

Phase 2 is scoped and sequenced in its own design iteration once phase 1 stabilizes.
