# Stability-first v2 (opinion)

**Audience:** Kyle Roche, staff engineer starting a conversation — not a ticket dump, not a rewrite-in-someone-else’s-image.

**Scope:** `griptape-nodes-engine` 0.99.0 (this repo). The published `griptape-nodes` / `gtn` app owns the daemon loop and most of the client-facing process. This memo is about the engine those processes load.

**Hard requirement (Kyle):** A v2 engine must be testable without touching the UI or other components. The contract to other components stays the same. Drop-in / side-by-side: point the existing IDE, desktop, CLI, and any websocket/HTTP clients at a v2 process and they still speak the same protocol.

**What this is not:** A line-by-line prescription. A plan to become Eve or fx. A proposal to rewrite the artist IDE.

**Fact vs guess:** Unlabeled claims are from this checkout. **HYPOTHESIS** marks inference. I did not invent metrics; where I could not measure, I say so.

---

## 1. How the engine actually works today

Griptape Nodes is two programs: a cloud/desktop **editor** and a local **application** that hosts this engine. The engine is not a separate deployment target. An editor never talks to “the engine” as a product; it talks to the application, which loads this library. See `docs/architecture.md` and `CONTRIBUTING.md`.

```
Editor (browser or Desktop)  --EventRequest JSON-->  Application (gtn / Desktop)
                                                       |
                                                       v
                                              Engine.handle_request()
                                                       |
                          +----------------------------+----------------------------+
                          |                            |                            |
                    ObjectManager                 FlowManager                  WorkerManager
                    (graph, params)            (ControlFlowMachine)         (optional subprocess)
                          |                            |                            |
                    in-memory nodes              in-memory FSM                 ExecuteNodeRequest RPC
```

### Process model

- **Long-lived orchestrator.** `Engine` (`src/griptape_nodes/retained_mode/engine.py`) is a plain class owning ~28 managers. Production uses one process root via `current_engine()`. Tests can construct many. `GriptapeNodes` is a thin global facade for generated workflow `.py` files, node libraries, and CLI entry — engine-internal code is banned from it (`TID251`).
- **Default: nodes run in the orchestrator process.** Worker isolation is opt-in. A library runs in-process unless it declares `SuggestedWorkerMode(mode=WORKER)` (`node_library/library_declarations.py` → `requires_worker_process`). Absence of the declaration means orchestrator.
- **Workers, when used.** `WorkerManager.spawn_worker` starts a subprocess with a fresh `GTN_ENGINE_ID`, registers over the WebSocket bus (`sessions/{session_id}/workers/{wid}/request|response`), and routes `ExecuteNodeRequest`. The worker builds a **transient** node, runs `aprocess`, ships outputs back, and discards the instance. Graph, connections, and parameter authority stay on the orchestrator. See `docs/development/custom_nodes/node_isolation_with_workers.md`.
- **Desktop vs CLI.** Same engine. Desktop bundles editor + application and uses a **direct WebSocket** so workflow events never leave the machine (`docs/architecture.md`). CLI `gtn` / `griptape-nodes` is the published app; the browser IDE and the engine each dial out to `wss://…/ws/engines/events?version=v2` (`api_client/client.py` → `get_default_websocket_url`). **HYPOTHESIS:** the daemon loop that drains `EventManager.event_queue` and the Desktop local WebSocket *listener* live in the `griptape-nodes` app, not this repo. This repo provides `Client`, `RequestClient`, `Engine`, and the payload types.

### How a graph runs

`StartFlowRequest` → `FlowManager.start_flow` → `ControlFlowMachine` → `ParallelResolutionMachine` (sequential is the same machine with `max_nodes_in_parallel=1`) → `NodeExecutor.execute` → `ExecuteNodeRequest` → local `aprocess` or worker RPC.

Cancel / step / continue are first-class requests (`CancelFlowRequest`, `SingleNodeStepRequest`, `ContinueExecutionStepRequest`). “Continue” means **unpause debug stepping**, not crash recovery (`retained_mode.py`).

Run state is **in memory**: the control-flow machine, per-node `NodeResolutionState` (`UNRESOLVED` / `RESOLVING` / `RESOLVED`), and `ObjectManager` objects. There is no execution ledger.

### What is durable today

| On disk | What it is | Not |
| --- | --- | --- |
| `{workspace}/**/*.py` | Saved workflows: PEP 723 `# /// script` metadata (`WorkflowMetadata.LATEST_SCHEMA_VERSION = "0.20.0"`) plus generated `build_workflow()` | Mid-run DAG position |
| `{workspace}/.env`, `~/.config/griptape_nodes/.env` | Secrets | |
| `griptape_nodes_config.json` (layered) | Config | |
| `{project}/project.yml` | Project template | Graph |
| `~/.local/share/griptape_nodes/engines.json` | Engine identities (`GTN_ENGINE_ID` selects) | |
| `~/.local/state/griptape_nodes/engines/{engine_id}/sessions.json` | Session ids and timestamps | Flow progress |
| Workspace files / static uploads | Assets | Live parameter objects |

Reload a saved `.py` and you get the graph and last **saved** parameter values. You do not get a half-run.

---

## 2. The frozen contract (do not change this for a first v2)

The IDE, Desktop, CLI, MCP clients, and worker subprocesses already share one language. That language is the v2 surface. Stability work happens **behind** it.

The protocol is large. That makes a v2 expensive to implement completely. It is **not** what makes the engine unstable. Do not invent a new protocol to “simplify.” If a first v2 cannot reimplement every handler on day one, keep a compatibility dispatcher that still accepts the same type names — a shim, not a new wire format.

### 2.1 Event types (the IDE’s RPC)

All operations go through dataclass payloads registered by **class name** in `PayloadRegistry` (`retained_mode/events/payload_registry.py`). The wire looks up `request_type` / `result_type` / `payload_type` and `cattrs`-structures the body (`event_converter.py`, `base_events.py` → `_resolve_payload_type`).

**Envelopes** (`retained_mode/events/base_events.py`):

| Envelope | Role |
| --- | --- |
| `EventRequest` | RPC: `request_type`, `request`, `request_id`, `response_topic` |
| `EventResultSuccess` / `EventResultFailure` | RPC result; `altered_workflow_state` tells the editor the graph is dirty |
| `EventRequestBatch` | N inner `EventRequest`s in one frame; results still come back one-by-one |
| `GriptapeNodeEvent` | Broadcast a result to connected clients |
| `ExecutionGriptapeNodeEvent` | Live run feed (`CurrentControlNodeEvent`, `ControlFlowResolvedEvent`, `ControlFlowCancelledEvent`, `InvolvedNodesEvent`, `ProgressEvent`, …) |
| `AppEvent` | Lifecycle (`EngineReadyEvent`, library loaded, config/secret changed, session, …) |

Every `BaseEvent` carries `engine_id` and `session_id` (class-var defaults mutated by `EngineIdentityManager` / `SessionManager`). The editor already multiplexes engines by those fields.

Counted from this checkout (class names ending in `Request` in `retained_mode/events/`, excluding infrastructure modules): **278 request types**, **545** `*ResultSuccess` / `*ResultFailure` classes. Largest families: `library_events` (30), `workflow_events` (30), `node_events` (24), `project_events` (20), `os_events` (18), `agent_events` (17). Schema dump helper: `generate_request_payload_schemas.py`.

Transport knobs on `RequestPayload` that the editor already uses: `broadcast_result`, `request_id`, `failure_log_level`, `fields` (dot-path filter on broadcast JSON). `SkipTheLineMixin` bypasses the queue for heartbeats.

**In-process entry (no UI):** `Engine.handle_request` / `ahandle_request`. Unit tests already construct `Engine()` and dispatch requests (`tests/unit/retained_mode/test_engine.py`, `tests/unit/retained_mode/managers/test_flow_manager.py`, `test_execute_node.py`, `tests/unit/retained_mode/events/`). This is the contract-test surface. Use it.

### 2.2 WebSocket

- URL: `{GRIPTAPE_NODES_API_BASE_URL}` with `http`→`ws`, path `/ws/engines/events?version=v2` (`api_client/client.py`). Auth: `Authorization: Bearer {GT_CLOUD_API_KEY}`.
- Publish shape: `{"type": event_type, "payload": payload, "topic": topic}`.
- RPC wrapper (`api_client/request_client.py`): `event_type=EventRequest` plus `request_type`, `request_id`, `request`, `response_topic`.
- Worker topics: `sessions/{session_id}/workers/{wid}/request` and `…/response` (`worker_manager.py`). Orchestrator and worker **must share an engine version** — “the wire shape of every event is tied to the engine build.”
- Large frames: warn at 100 KB, **do not block** (TODO cites `#4124`).

Desktop’s direct WebSocket is the same event JSON on a local listener. **HYPOTHESIS:** listener implementation is in the app package. Freeze the JSON, not the TCP port the app chooses.

### 2.3 HTTP routes in this repo

Static server (`servers/static.py`), defaults `localhost:8124`, env `STATIC_SERVER_HOST` / `STATIC_SERVER_PORT` / `STATIC_SERVER_URL` (`/workspace`) / `STATIC_SERVER_ENABLED`:

| Method | Path |
| --- | --- |
| POST | `/static-upload-urls` |
| PUT | `/static-uploads/{path}` |
| GET | `/static-uploads/` and `/static-uploads/{prefix}` |
| DELETE | `/static-files/{path}` |
| GET | `/api/libraries/{library_name}/widgets/{path}` |
| GET | `/external/{path}` |
| GET | `{STATIC_SERVER_URL}/*` (workspace) and legacy `/static/*` |

CORS allowlist includes `https://app.nodes.griptape.ai`, staging/nightly, `http://localhost:5173|5174`, `gtn-editor://editor`.

MCP server (`servers/mcp.py`), defaults `localhost:8125` (`GTN_MCP_SERVER_HOST` / `GTN_MCP_SERVER_PORT`), streamable HTTP at `/mcp/`. Tools are a **subset** of the same request class names (~40 in `SUPPORTED_REQUEST_EVENTS`) plus a batch tool. Handler calls `GriptapeNodes.handle_request()` — same engine, no second protocol.

Existing tests: `tests/unit/servers/test_static.py`, `tests/unit/servers/test_mcp.py`.

### 2.4 CLI surface that lives here

This repo is not the `gtn` assembler, but the app imports Typer commands from `src/griptape_nodes/cli/`:

- `init` — `--api-key`, `--workspace-directory`, `--storage-backend`, `--non-interactive`, HF token, library registration
- `config` — show / list / reset
- `libraries` — sync / download
- `models` — download / list / search
- `self` — version / info / uninstall
- `doctor` — including `doctor/websocket_connection.py`

Env already used for init: `GTN_WORKSPACE_DIRECTORY`, `GTN_API_KEY`, `GTN_STORAGE_BACKEND`, `GTN_REGISTER_DIFFUSERS_LIBRARY`, `GTN_LIBRARIES_SYNC`, `GTN_BUCKET_NAME`.

Keep flags and subcommand names. A v2 process launched as `gtn` (or `gtn --engine-impl v2` if the **app** grows a switch) must still accept `gtn init` and the rest.

### 2.5 Workspace / session files the IDE depends on

Do not relocate or rename these for a first v2:

- `~/.config/griptape_nodes/griptape_nodes_config.json` and workspace overlays
- `~/.config/griptape_nodes/.env` and `{workspace}/.env`
- `{workspace}/**/*.py` workflow files with `# /// script` metadata and `schema_version`
- `{project}/project.yml` (`project_template_schema_version`)
- `~/.local/share/griptape_nodes/engines.json`
- `~/.local/state/griptape_nodes/engines/{engine_id}/sessions.json`
- Workspace asset layout served by the static server
- Library JSON (`griptape_nodes_library.json` / `griptape-nodes-library.json`) and `library_schema_version` (currently `0.11.0` in `LibrarySchema`)

### 2.6 Artist-facing behavior that *is* the contract

The editor’s run buttons are not “UI details.” They are requests this engine already implements, documented in `docs/guides/editor/running_workflows.md`:

- Run Workflow → `StartFlowRequest`
- Run To Selected → resolve that node and unresolved upstream
- Run From Selected → `StartFlowFromNodeRequest`
- Cancel Run → `CancelFlowRequest` → `ControlFlowCancelledEvent`
- Status pills: Running / Resolved / Error / Unresolved ← `NodeResolutionState` + execution events
- Dirty-document asterisk ← `altered_workflow_state`
- Cannot save while a flow is running

A v2 that is “more stable” but cannot step, inject a node mid-run, or paint those pills has broken the product even if the WebSocket still connects.

### 2.7 In-flight contract extensions (do not fight them)

Open work on this repo is already adding **more** protocol, not less: execution-lease events (`#5308`–`#5312`), bounded reconnect (`#5344`), block-large-payloads (`#4124`). If those land in v1, they become part of the frozen surface. A v2 implements them. It does not replace them with a different admission API.

---

## 3. Where stability breaks

### Process death takes the session

**FACT.** Most libraries execute in the orchestrator. A native crash (CUDA OOM, segfault in `torch`, runaway C extension) has no engine-level handler. **HYPOTHESIS:** the OS kills the orchestrator; every in-flight run, every non-worker library, and the editor connection die together.

`LibraryRegistry` is process-global on purpose — node classes live in `sys.modules`. `WorkflowRegistry` is still a process singleton. `BaseEvent._session_id` is a class attribute. One poisoned import is everyone’s import.

Worker mode helps **that library’s** `process()`, not the control plane. Orchestrator still holds the graph, default libraries, the static server thread, and `INCOMPATIBLE` libraries.

Desktop documents the blast radius in the other direction: quitting the app “stops the engine, which cancels anything still running” (`docs/guides/desktop/app_settings.md`). Process lifetime **is** session lifetime.

### IDE disconnect is not a defined engine policy

**FACT.** `SessionManager.handle_session_end_request` clears `sessions.json`. It does not call `cancel_flow_run()` or reset workers. No handler in this package maps WebSocket `ConnectionClosed` to cancel.

**HYPOTHESIS.** Disconnect usually leaves runs going until an explicit `CancelFlowRequest`, process death, or a worker heartbeat eviction (5s pulse / 15s silence) fails an in-flight `ExecuteNodeRequest`. That is the *opposite* of “reconnect kills work” — and also the opposite of a durable run the editor can reattach to. The artist sees a dead canvas and a live GPU job, or a cancelled job they did not cancel, depending on the app layer. This engine does not own a single written policy.

Desktop quit is the one documented cancel-on-disconnect: it kills the process.

### No durable run ledger

**FACT.** `continue_flow` is unpause. `reset_flow` unresolves every node. Workflow save is graph + parameter values (including pickle of unique values in `workflow_manager.py`), not “node N of run R completed with output at path P.” There is no checkpoint, no run id in the execution machine that survives `current_engine()` dying.

After a crash the honest recovery is: reopen the `.py` if it was saved, reset, run again.

### Cancel does not beat late success

**FACT.** `cancel_all_nodes` sets a cooperative flag (`is_cancellation_requested()`), fire-and-forgets `CancelExecuteNodeRequest` to workers, then cancels asyncio tasks. Nodes that do not poll the flag keep running. `handle_done_nodes` marks `RESOLVED` and publishes parameter updates if the task finishes first. `ErrorState` treats `task.done()` as `DONE`, not `CANCELED`. Worker late JSON becomes unmatched traffic (orchestrator already cancelled the future) — better than applying — but **orchestrator-local** late success can still resolve a node after the user hit Cancel.

This is not fx’s “cancellation wins over late success.” It is best-effort.

### Unbounded memory

**FACT.** Parameter values are live Python objects. Worker hydration has no size cap. Workflow codegen pickles unique values into the `.py`. Static `PUT /static-uploads/{path}` writes the entire body with no limit. WebSocket send warns at 100 KB and still sends. No engine-side cache eviction for the static mount.

**HYPOTHESIS.** Long image/video/tensor sessions grow RSS until the orchestrator is the OOM victim. I have no measurements.

### Libraries crash or hang the host

**FACT.** Non-worker libraries import on the orchestrator (`LibraryManager._attempt_load_nodes_from_library`). Lazy load only delays the import. Schema probe for worker libraries instantiates each class once, **10s timeout**, skip-on-hang, **silent drop** if `__init__` raises. `reentrant-bus-in-init` is a documented deadlock. Successful import side effects (CUDA init) are already in the process.

### Event loop pressure

**FACT.** One asyncio loop owns request handling. Post-dispatch hooks are unbounded (warn at 100 in-flight). Cross-thread work uses `call_soon_threadsafe`. Worker RPC has no per-request timeout — liveness is heartbeat only. `#4469`-style wrong-loop deadlocks are commented in `event_manager.py`.

**HYPOTHESIS.** Sync work on the engine loop stalls the WebSocket and looks like disconnect to every client.

### Hard-to-resume graphs

**FACT.** Mid-run inject requires unresolved state or the completion is ignored as duplicate. Iterative groups have their own `reset_for_workflow_run`. `ParallelResolutionContext.generation` already exists to abandon stale drivers after teardown — it is an in-memory generation counter, not a durable run id.

A half-cancelled loop is not a defined resume point.

### Issue tracker (could not measure a crash cluster)

GitHub’s repo API reports `open_issues_count: 735` (issues **and** PRs; the prompt’s “719+” is the same order of magnitude). The token available to this pass could only *search* a small subset (~32 open issues). Keyword search on that subset does **not** show a documented crash/hang/reconnect cluster. I will not invent one.

What *is* visible and on-topic: cancel-while-queued / lease lifetime (`#5312`), bounded reconnect (`#5344`), block large messages (`#4124`), execution-lease protocol (`#5308`–`#5310`). The team is already treating multi-engine admission and reconnect as protocol problems. That is the right layer.

---

## 4. What to keep

Do not throw away the thing artists bought.

1. **The node graph.** `BaseNode`, parameters, connections, control vs data, node groups, iterative start/end. This is the product.
2. **The event contract above.** Same type names, same envelopes, same `altered_workflow_state`, same execution pills.
3. **Saved workflow `.py` files** with `schema_version` 0.20.x. Artists already have them. Codegen can evolve behind the header; do not require a new document format to get stability.
4. **Workspace as the file root.** Secrets in `.env`, assets on disk (or opted-in GTC), project.yml. Studios mount NAS here on purpose.
5. **Library JSON + per-library venv.** Isolation of *dependencies* is already the right idea. The bug is that isolation is opt-in and incomplete.
6. **Retained Mode / `handle_request`.** Scripts, MCP, tests, and the IDE are the same API. That is how v2 stays testable without a UI.
7. **Run To / Run From / inject-during-run / single-step.** These are not debugger toys; they are how artists iterate.
8. **Engine identity (`GTN_ENGINE_ID`) and session ids.** The editor already knows there can be more than one engine. Use that for A/B.

---

## 5. What to gut (behind the contract)

Gut the **implementation assumptions**, not the artist surface.

1. **“The orchestrator is a fine place to run `torch`.”** Make the control plane boring. Heavy and untrusted node code leaves the process. Default today’s opt-in worker path; keep Shared mode as an escape hatch for nodes that *must* hook editor-time connection APIs (those hooks are already inert on workers — documented).
2. **In-memory run state as the source of truth.** Keep it as a cache. The source of truth for a run should be an append-only ledger the process can lose.
3. **Process-global registries where they advertise isolation they do not have.** `LibraryRegistry` being process-global is honest (modules are). `WorkflowRegistry` should be engine-owned (already noted in `CLAUDE.md`). Session id as a class var on `BaseEvent` is a landmine once two engines exist in one test process — tests already fight this.
4. **Cooperative-only cancel.** Keep the flag for polite nodes. Add a generation gate so a late `RESOLVED` from generation N cannot apply after cancel of generation N. Cancellation wins. Late worker success is dropped, not painted green.
5. **Silent schema-probe drops and `__init__` I/O.** A class that times out or deadlocks the bus must fail the library load loudly, not vanish from the palette.
6. **Unbounded blobs on the live bus.** `#4124` is correct: cap WebSocket frames. Cap static uploads. Prefer file/URL artifacts in parameters over pickled tensors in the workflow file for *live* execution. (Saved `.py` pickle is a compatibility constraint; stop making it worse.)
7. **Reconnect as an undefined side effect.** Write the rule in the engine: dropping the editor does **not** cancel a run; `CancelFlowRequest` and process teardown do. Reattach replays the ledger. Desktop quit remains “kill the process” unless Desktop grows a detach mode — that is an app change, not an IDE rewrite, and it is not required for a first v2.

Do **not** gut: the 278 request names, the static URL layout, MCP tool names, workflow file format, or the canvas run model.

---

## 6. v2 stability thesis

1. **Orchestrator is a control plane.** It owns the graph, the ledger, secrets, and the frozen event dispatcher. It does not import `diffusers`. Node `process()` runs in a child (library worker today; tighter sandbox later). Child death fails that node, not the session.
2. **Durable run ledger.** Every run has an id. Every node attempt appends: inputs hash, output artifact refs, status, generation. Workflow `.py` remains the *document*. The ledger is the *performance*. Lose the process, keep the performance.
3. **Reconnect ≠ cancel.** An editor drop is a stream cursor problem. `CancelFlowRequest` is the only artist cancel. Session heartbeat can *detect* a dead seat; it must not silently mean cancel unless we document that and the IDE shows it.
4. **Generation-gated apply.** `ParallelResolutionContext.generation` already exists. Persist it. Ignore results whose generation is stale. Cancel increments generation. Late success cannot win.
5. **Sandbox per library first, per graph when the graph is untrusted.** Per-node VMs are the wrong first cut for an artist tool (startup tax, GPU residency). Per-library workers already match how people pin `torch`. Add per-graph isolation for “run this stranger’s workflow.” Do not sandbox the control plane’s need to serve previews and answer `GetAllNodeInfoRequest` in milliseconds.
6. **Crash recovery is replay, not hope.** On boot: read ledger, mark interrupted attempts failed-or-retry per node idempotency, restore `RESOLVED` from artifact refs, tell the editor the truth via the existing execution events. Do not pretend a half-written video file is a success.
7. **Observability is part of stability.** Heartbeat already returns version / session / workflow. Add run id, generation, worker liveness, hook in-flight count, ledger head. If we cannot see it, we will not know whether v2 is better. I have no baseline metrics today.
8. **Same protocol, two processes.** v1 and v2 A/B on one machine without an IDE build. If we cannot do that, we are not ready to call it v2.

---

## 7. Drop-in / side-by-side (no UI change)

The editor already selects an engine by `engine_id` / name (`GetEngineNameRequest`, `engines.json`, `EngineReadyEvent`). The application already overlays this checkout on the published app (`make run`, `uv tool install griptape-nodes --with-editable .`). Use those, do not add a “v2 canvas.”

### How to run both

| Knob | v1 (today) | v2 (same contract) |
| --- | --- | --- |
| Binary | `gtn` / `griptape-nodes` / Desktop-bundled app | Same binary, different engine implementation loaded into it — **or** the same app pointed at a v2-capable install |
| Engine identity | `GTN_ENGINE_ID` unset → default in `engines.json` | `GTN_ENGINE_ID=<uuid-or-name-for-v2>` so the IDE lists two engines |
| Overlay | published wheel | `uv tool install griptape-nodes --python 3.12 --with-editable /path/to/v2 --force` (same path used for engine development today) |
| Nodes API | `GRIPTAPE_NODES_API_BASE_URL` (default `https://api.nodes.griptape.ai`) | **Same.** Both dial `/ws/engines/events?version=v2` |
| Auth | `GT_CLOUD_API_KEY` | Same key. Do not invent a v2 auth |
| Static HTTP | `STATIC_SERVER_PORT=8124` | `STATIC_SERVER_PORT=8224` (or any free port) if both run on one host |
| MCP HTTP | `GTN_MCP_SERVER_PORT=8125` | `GTN_MCP_SERVER_PORT=8225` |
| Workspace | artist’s workspace | Same workspace **or** a copy. Same engine id + same workspace = file fights. Different `GTN_ENGINE_ID` is the isolation that already exists |
| App switch **HYPOTHESIS** | none | Smallest *app* flag, not an IDE change: `gtn --engine-impl v2` or `GTN_ENGINE_IMPL=v2`, implemented in `griptape-nodes` if the v2 code is a second package. Until then, editable overlay is enough |

Desktop: point the bundled app at a v2-capable engine the same way developers already do (editable overlay / pinned interpreter). Do not ship a second editor.

### How to test without the UI

This is the acceptance bar, not a nice-to-have.

1. **In-process contract tests.** Construct `Engine()`, `handle_request(CreateFlowRequest(…))`, create nodes, connect, `StartFlowRequest`, assert `EventResultSuccess` types and execution payloads. `tests/unit` already does this shape. A v2 that cannot pass a copied v1 contract suite is not a drop-in.
2. **Wire golden files.** Serialize `EventRequest` / `EventResult*` / `ExecutionGriptapeNodeEvent` to JSON; assert `request_type` strings and envelope keys. `tests/unit/retained_mode/events/` is the seed.
3. **HTTP contract tests.** Replay `tests/unit/servers/test_static.py` and `test_mcp.py` against a v2 process. Same paths, same status codes, same JSON keys.
4. **Headless executors.** `bootstrap/workflow_executors/` already run `.py` workflows without a canvas. Use them as soak tests.
5. **Two-process A/B.** Start v1 and v2 with different `GTN_ENGINE_ID` and ports. Drive both with the same `RequestClient` script. Compare result types, not pixels.

If a handler is not yet reimplemented, the compatibility shim returns the **same** `*ResultFailure` shape the IDE already renders — or forwards to a v1 in-process fallback. It does not hang, and it does not send a new event family.

### Smallest shim (only if needed)

If we discover a specific request whose *semantics* prevent isolation (example: a node library that must mutate orchestrator state synchronously during `process`), do not redesign the IDE. Add a documented compatibility mode: that library stays Shared / in-process, same as today. The shim is “this request still hits the old kernel,” not “here is protocol v3.”

The current contract does **not** appear to be the instability. The instability is that the kernel behind `StartFlowRequest` shares a process with `torch` and forgets the run when that process dies.

---

## 8. Eve / fx — what transfers, what does not

These are ideas, not a target architecture. This is a visual node engine for artists, not a coding-agent harness.

### Transfers

| Idea | Why it maps |
| --- | --- |
| **Durable session / run distinct from the socket** (Eve: reconnect from stream cursor; crash resumes last completed step) | Our editor already reconnects to an engine id. We lack the cursor and the journal. |
| **Control plane ≠ sandbox** (Eve: workflow in the app runtime, tools in a sandbox; fx: child death ≠ parent death) | We already drew this line (orchestrator / worker) and then defaulted most libraries onto the parent. |
| **Cancellation wins; late success is ignored** (fx) | We have the race today in `handle_done_nodes`. |
| **Idempotent outcomes + recorded results** (Eve: completed steps never re-run; interrupted steps re-run) | Image/video nodes often are *not* idempotent (seeded, billed, overwrite files). The ledger must record artifact refs so replay does not re-bill; nodes that cannot be replayed safely must be marked so. |
| **Owner / slot isolation** (fx) | One artist’s library worker must not kill another artist’s orchestrator. Execution-lease work (`#5308+`) is already the multi-seat version of this. |
| **Filesystem as the durable store** (Eve workspace; fx `communication.json`) | We already trust the workspace for documents and assets. Put the run ledger *next to* the workflow (or under XDG state keyed by `engine_id` + run id). Do not put it only in RAM. |

### Does not transfer

| Idea | Why it does not |
| --- | --- |
| **Filesystem-first agent authoring** (`agent/` tree, skills, channels) | Artists author graphs on a canvas, not a repo of tools. Workflow `.py` is a save format, not the editing model. |
| **LLM turn / step as the unit of durability** | Our unit is a **node resolve**. A turn-shaped journal would fight Run To Selected and mid-run inject. |
| **Park the whole session on human approval as the default** | We already pause for debug step. Most nodes should run. Do not make every image gen a durable workflow-SDK step with four retries unless the node opts in. |
| **Sandbox the artist’s whole workspace as untrusted** | The workspace *is* their studio (NAS, existing plates, fonts). Isolate *code*, not their files, unless they run a foreign graph. |
| **Replace EventRequest with Eve’s HTTP session API** | That would be an IDE rewrite. Forbidden. |
| **Tiny native harness as the product** | fx is a harness. We are a retained-mode graph + media server + library loader. The harness ideas apply to the *run*, not the *editor contract*. |

If we take one sentence from Eve: *lose the process, keep the run, reconnect the stream.* If we take one sentence from fx: *a child’s death is not the parent’s death, and cancel beats a late OK.*

---

## 9. Opinion, in one page

v2 is not a new product. It is a different kernel behind a frozen `handle_request`.

Ship nothing that requires an IDE change to prove. Prove it with `Engine()` tests, HTTP tests, and two `gtn` processes with different `GTN_ENGINE_ID`. Point `app.nodes.griptape.ai` at the v2 engine the way a developer already points it at this checkout.

The first stability win is boring: **node code cannot kill the session, a dropped websocket cannot be confused with cancel, and a crash leaves a ledger the next process can tell the truth from.** Everything else — prettier workers, Eve-shaped sandboxes, per-node VMs — is later, and only if it still speaks `StartFlowRequest`.

I would discard any part of this memo that a week of contract tests contradicts. The code in this repo is the source of truth; Eve and fx are reference ideas; the artist canvas is the product we are not allowed to surprise.
