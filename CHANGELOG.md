# Changelog

All notable changes to Griptape Nodes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
**Breaking** marks a change that can stop a saved workflow, a node library, or a client of
the engine's request API from working without edits. Migration steps live in
[MIGRATION.md](MIGRATION.md).

## [Unreleased]

<!-- Entries go in changelog.d/, one file each. See changelog.d/README.md. -->

## [0.105.0] - 2026-10-27

### Added

- Engine processes now read `GTN_APPEND_PYTHONPATH`, a list of folders added to the end of the
  module search path. Modules in those folders can be imported from nodes, but never replace the
  engine's own packages the way `PYTHONPATH` entries can.
- `CreateConnectionResultSuccess` now has a `replaced_connection` field, and its message names the
  connection, when the new connection replaced one on an input or control output that allows only
  one connection.
  [#5912](https://github.com/griptape-ai/griptape-nodes-engine/issues/5912)
- `CreateNodeResultSuccess` now has a `paired_node_name` field naming the End node that creating
  a For Each or For Loop Start node also creates, so clients don't create a second one.
  [#5911](https://github.com/griptape-ai/griptape-nodes-engine/issues/5911)
- A `DatePicker` trait gives a `str` parameter a calendar picker in the editor. The value is an
  ISO 8601 local date such as `2026-10-14`, or `2026-10-14T09:30` with `include_time=True`, so a
  node can read it with `date.fromisoformat`. Optional `min` and `max` bound the allowed dates.
  [#5890](https://github.com/griptape-ai/griptape-nodes-engine/issues/5890)
- Nodes that call a model provider directly, rather than through Griptape Cloud, can now take part
  in Griptape Cloud spending budgets. `BudgetAccessRequest` asks Cloud before the call and fails
  with the same "Budget stopped this run." message when a budget refuses it; if Cloud cannot be
  reached the call goes ahead. `ReportUsageRequest` sends the call's token counts afterwards, in the
  background, and Cloud prices them. See [Budgets for Direct Provider Calls](https://docs.griptapenodes.com/en/stable/development/custom_nodes/direct_provider_budgets/).
  [#5422](https://github.com/griptape-ai/griptape-nodes-engine/issues/5422)
- An "Isolated Execution in a worker" beta feature runs the nodes of groups and loops set to
  "Isolated Execution" in one background process that stays open for the session, instead of
  starting a new process each time the group or loop runs. The group's results no longer travel
  through Griptape Cloud, so Isolated Execution works on installs that can't reach it. Turn it on
  from the editor's Beta settings page.
- Advanced libraries can return execution and app event listeners from
  `get_execution_event_listeners()` and `get_app_event_listeners()`, for example to log or time
  every node a workflow runs. The engine registers them when the library loads and removes them
  when it unloads. See the
  [timing example](docs/development/custom_nodes/advanced_libraries.md#example-a-library-that-times-every-node-run).
  [#5779](https://github.com/griptape-ai/griptape-nodes-engine/issues/5779)
- Each `successful_libraries` entry from `LoadMetadataForAllLibrariesRequest` now reports how the
  engine's last attempt to load that library went, as `lifecycle_state`, `fitness`, `problems`,
  and `execution_env_failure`, so a library that failed to load can be told apart from one waiting
  for a refresh.
  [#5102](https://github.com/griptape-ai/griptape-nodes-engine/issues/5102)
- `ListNodeTypesInLibraryRequest` takes `include_metadata` to also return each node type's category,
  display name, description, and deprecation. MCP clients can survey every library's node types in
  one `EventRequestBatch` instead of reading library manifests off the engine's disk. It reads library
  metadata only, so it loads no node code.
  [#5962](https://github.com/griptape-ai/griptape-nodes-engine/issues/5962)
- New setting `max_loop_iterations_in_parallel` limits how many iterations of a loop set to
  "Run Group Items All at Once" run at the same time. Defaults to 0 (no limit).
  [#5067](https://github.com/griptape-ai/griptape-nodes-engine/issues/5067)
- MCP clients can now call `CancelFlowRequest` to stop a running flow and `UnresolveFlowRequest` to
  reset a flow's nodes, so an agent can recover a stuck run without reloading the workflow.
  [#5754](https://github.com/griptape-ai/griptape-nodes-engine/issues/5754)
- MCP clients can now call `ValidateFlowDependenciesRequest` to run a flow's pre-run checks without
  running it, so an agent can find wiring and configuration errors before spending a run on them.
  [#5917](https://github.com/griptape-ai/griptape-nodes-engine/issues/5917)
- Node libraries can build a `NodeError` for a failed provider call with `NodeError.from_missing_secret`,
  `NodeError.from_http_response` and `NodeError.from_request_exception`, instead of each library
  writing its own helpers. A refused API key links to the secret in Settings, and timeouts and
  failed connections get a plain message.
  [#5899](https://github.com/griptape-ai/griptape-nodes-engine/issues/5899)
- `NodeStartProcessEvent` is sent again before every node runs, and `NodeFinishProcessEvent` after,
  for every node the engine runs: regular nodes, nodes that run in a worker, group and loop nodes,
  and the nodes inside them. Both now carry `node_type`, `specific_library_name`, `flow_name`,
  `execution_id`, and `parent_execution_id`, and `NodeFinishProcessEvent` says how the run ended
  (`outcome`, `error_message`, `error`). Loop body nodes are named as on the canvas.
  [#5779](https://github.com/griptape-ai/griptape-nodes-engine/issues/5779)
- `ParameterList` takes `item_converters`, which convert each item whether it is set in its own row
  or arrives in a whole list. A whole list a node sets on its own list with `set_parameter_value`
  now becomes the list's items, as it does when set by request. See
  [Parameters](docs/development/custom_nodes/parameters.md).
- The engine now watches each local file the editor previews and sends a `StaticFileChanged` event
  when the file is saved, including saves from other apps, so the editor can show the new content.
  Turn it off with the `refresh_previews_on_file_change` setting, and cap how many files are watched
  with `refresh_previews_max_files` (1000 by default).
  [griptape-vsl-gui#3273](https://github.com/griptape-ai/griptape-vsl-gui/issues/3273)
- `ResetLibraryRequest` deletes a library's Python environments (`.venv` and `.venv-exec`) and
  builds them again, so a library whose packages are broken can be recovered without deleting
  folders by hand. When the engine has already loaded packages from the library, the reset finishes
  the next time the engine starts and the result reports `restart_required`.
  `LoadMetadataForAllLibrariesRequest` reports `reset_requires_restart` so a client can say so
  before the reset is confirmed.
  [#5102](https://github.com/griptape-ai/griptape-nodes-engine/issues/5102)

### Changed

- **Breaking:** The chat agent now asks before it runs workflows or nodes, generates images, or uses
  tools from your own MCP servers, so a question no longer spends credits unprompted. Such a turn
  ends `RunAgentRequest` with the calls in `output.pending_approvals` instead of running them, and
  they run only once a client answers with `ResolveToolApprovalsRequest`; see
  [Approvals](docs/guides/agent/approvals.md).
  [#5606](https://github.com/griptape-ai/griptape-nodes-engine/issues/5606)
- **Breaking:** The "Private Execution" option for groups and loops is now called "Isolated
  Execution": it runs them in a separate process, which keeps them apart but does not make them
  private. Workflows saved with "Private Execution" open with "Isolated Execution" selected. A
  workflow saved after this change opens in an older version with "Local Execution" selected. Node
  libraries that import `PRIVATE_EXECUTION` from `griptape_nodes.exe_types.node_types` need to use
  `ISOLATED_EXECUTION` instead.
- Opening, pasting, and editing large workflows spend less time after each request as the workflow
  grows. The engine used to check every node for pending editor updates after each request; it now
  checks only the nodes that changed.
- The editor no longer stalls while the engine saves a large image with its workflow metadata or
  makes an image preview.
- Loading libraries, at engine startup and on a library reload or project switch, no longer runs
  git several times per library or re-reads every package installed in the engine's environment.
  With three libraries installed, a reload takes about 0.2 seconds instead of 1.9.
  [#5940](https://github.com/griptape-ai/griptape-nodes-engine/issues/5940)
- A `NodeError` response over 16 KB now keeps its structure, with base64 data replaced by its length
  and strings over 2000 characters cut, instead of being dropped. It is dropped only if it is still
  too large.
  [#5899](https://github.com/griptape-ai/griptape-nodes-engine/issues/5899)
- Running a workflow spends less time between nodes. Before each node runs, the engine reads the
  project's variables, and it now works out `{workflow_dir}` once per read instead of once for
  every project directory.

### Fixed

- The sidebar agent no longer fails with "Request timed out" behind proxies that hold the TLS
  handshake for longer than 5 seconds. Model requests now allow 60 seconds to connect.
  [#5768](https://github.com/griptape-ai/griptape-nodes-engine/issues/5768)
- A chat message that reaches the agent's limit on steps per message no longer disappears from the
  conversation with an error. The agent's work up to the limit stays in the conversation, and the
  reply says it stopped, so sending "continue" picks up where it left off.
  [#5908](https://github.com/griptape-ai/griptape-nodes-engine/issues/5908)
- Griptape Cloud storage and file requests now keep the path in `GT_CLOUD_BASE_URL`. A base URL like
  `https://example.com/prefix` sent requests to `https://example.com/api/...`, dropping `/prefix`.
  [#5789](https://github.com/griptape-ai/griptape-nodes-engine/issues/5789)
- A value that reaches a node over a connection is no longer run through variable substitution
  again when the node copies it onto its own output. A save node's `Saved to: {outputs}/a.mp4`
  status wired into an End node now stays a macro on the End node too, so the saved workflow
  holds no absolute path. [#5897](https://github.com/griptape-ai/griptape-nodes-engine/issues/5897)
- The `execution_events` panel of a group set to Isolated Execution or to a library's environment
  no longer shows every event twice. It shows a "Starting:" line for each node again, and a
  "Finished:", "Failed:", or "Cancelled:" line when the node ends.
  [#5779](https://github.com/griptape-ai/griptape-nodes-engine/issues/5779)
- The engine reports that it is still initializing from the moment it starts, not only once library
  loading begins. An editor that connects during startup now waits for the engine instead of
  showing a connection error.
  [griptape-nodes-desktop#466](https://github.com/griptape-ai/griptape-nodes-desktop/issues/466)
- Library templates no longer disappear from the editor's library filters after switching
  projects. Switching now re-registers each library's templates against the new project.
  [#5765](https://github.com/griptape-ai/griptape-nodes-engine/issues/5765)
- **Create** on a workflow a library provides now makes a copy in your workspace even when the
  library did not mark it `is_template`, and saving an open library workflow writes a copy instead
  of overwriting the library's file. Before, **Create** opened the library's own file for editing.
- Removing or moving an item of a list parameter now updates the list that
  `GetParameterValueRequest` returns, reruns the nodes that read it, and lets the node react, as
  "Path Join" does when it recomputes its output. A node whose library runs in a separate process
  now receives its list inputs instead of an empty list.
- A row of a list parameter (for example `reference_images` on "Kling Omni Video Generation") now
  accepts a whole list, such as the output of "Create Image List", and adds its items to the list.
  [#2943](https://github.com/griptape-ai/griptape-vsl-gui/issues/2943)
- Workflows with a chain of more than about 1,000 connected nodes now run and accept edits. Before,
  passing a value down the chain failed with "maximum recursion depth exceeded". Passing values
  down long chains also takes less time.
- A `ForEach Group`, `For Loop Group`, or `Retry Group` whose body holds a node with a list
  parameter wired row by row from other nodes, such as `Merge Images`, no longer fails at the end of
  the run with "Attempted to set list ... some of its items are connected to other nodes".
  [#6013](https://github.com/griptape-ai/griptape-nodes-engine/issues/6013)
- Creating a For Each or For Loop Start node with `CreateNodeRequest` and no `position` in its
  `metadata` now creates and connects its End node, instead of failing with `'position'`.
  [#5910](https://github.com/griptape-ai/griptape-nodes-engine/issues/5910)
- Opening a workflow by running a file the registry doesn't know (for example, one outside the
  workspace) no longer shows a "GetWorkflowMetadata Failed" error. Its metadata is read from the
  file's header.
  [#5794](https://github.com/griptape-ai/griptape-nodes-engine/issues/5794)
- A node failing in a parallel run no longer cancels the other nodes running alongside it. Nodes
  that already started finish and show their results, nodes still waiting to start are skipped, and
  the run then reports the failure. Cancelling a run still stops every node.
  [#5893](https://github.com/griptape-ai/griptape-nodes-engine/issues/5893)
- A loop set to "Run Group Items All at Once" no longer floods the editor with events about the
  temporary copies of its body, which could make the editor report the engine as disconnected.
  [#5067](https://github.com/griptape-ai/griptape-nodes-engine/issues/5067)
- Switching projects no longer freezes the editor for about 25 seconds and stops a library's nodes
  from running for the rest of the session, when that library runs its nodes in a separate process
  and the new project's workspace contains saved workflows.
  [#5946](https://github.com/griptape-ai/griptape-nodes-engine/issues/5946)
- Saving a workflow right after updating one of its libraries now keeps parameter values whose
  type comes from that library, such as a node's mode dropdown. Before, the save dropped those
  values with a warning, and engines before 0.104.0 failed to save with "Missing stable
  namespace".
- A node's status no longer has project variables substituted into it during a run, so a status
  such as `Saved to: {outputs}/a.mp4` is saved with the workflow as written instead of as the
  absolute path. [#5897](https://github.com/griptape-ai/griptape-nodes-engine/issues/5897)
- Closing a workflow that contains node groups, such as a ForEach Group, no longer logs a
  "subflow doesn't exist" warning for each group.
  [#5746](https://github.com/griptape-ai/griptape-nodes-engine/issues/5746)
- Setting a whole list on an unconnected list parameter by request (an MCP client, an agent, a
  script) now fills the list's items, the same as entering them one by one in the editor. The
  request used to succeed while the node still read an empty list.
  [#5758](https://github.com/griptape-ai/griptape-nodes-engine/issues/5758)
- A node whose library runs in a separate worker process no longer waits up to 10 minutes when
  that process stops while starting up, for example because no license session was available
  for it. The run now fails right away and says that the process stopped and where to look.
- Running a node whose library runs in a separate process no longer deletes connections when the
  node reacts to a value by changing its wires, such as `Load Pipeline Component` in the Modular
  Diffusion library dropping every wire it fed after a run.
- A library whose nodes run in a worker process now starts its worker as soon as the library
  finishes installing. Its runs no longer fail with a "No module named" error after a first install
  until the engine restarts, and a library installed from Git while the editor is connected no
  longer goes without a worker until **Refresh Libraries**.
- Workflows behind a library's `workflow_nodes`, such as the Modular Diffusion "Preconfigured"
  nodes, are now listed under their library as soon as the library loads. Before, they appeared
  only after the node was placed, and a workspace change removed them again.

## [0.104.2] - 2026-10-20

### Fixed

- The agent chat on Windows no longer fails on every message with "`LocalWorkspaceBackend` only
  supports POSIX platforms" when the workspace has an `.agents/skills` folder. If a platform
  cannot load skills, the chat now answers without them instead of failing.

## [0.104.1] - 2026-10-13

### Fixed

- Unlocking a node that was locked before it ever ran now lets the next downstream run execute it.
  A run passing through a locked node marks it resolved without running it, and that state used
  to outlive the unlock, so the node was skipped until it was run directly.
  [#5766](https://github.com/griptape-ai/griptape-nodes-engine/issues/5766)
- The editor no longer shows a "GetConfigValue Failed" popup for `library.sandbox_enabled` at
  launch or on refresh. `GetConfigValueRequest` now succeeds with an empty value for a setting
  that is unset by default, and fails only for a key that names no setting.

## [0.104.0] - 2026-10-12

### Added

- The editor's "Enable beta features" switch now turns engine and library beta features off too,
  not only editor ones. It is saved as `beta_features.enabled`. Each feature keeps its own setting
  and gets it back when the switch is turned on again. `GTN_CONFIG_BETA_FEATURES__ENABLED=false`
  turns every beta feature off for one session.
  [#5710](https://github.com/griptape-ai/griptape-nodes-engine/issues/5710)
- With Griptape Cloud spending budgets, a node whose call a budget refuses fails with a message
  naming the node and every budget that refused, and the editor's "Run blocked" bar shows it, for
  example: "Budget stopped this run. 'Generate Poster' was blocked by the budget "Marketing Q3".
  Contact your Griptape administrator." A node with its Failure output wired takes that branch, as
  it does for any other error. Sidebar chat replies and the images they generate count against
  the open project's budgets, and a refusal there ends the reply instead. See
  [When a budget stops a run](https://docs.griptapenodes.com/en/stable/guides/editor/running_workflows/#when-a-budget-stops-a-run).
  [#5422](https://github.com/griptape-ai/griptape-nodes-engine/issues/5422)
- `GTN_ENGINE_CONFIG_DIR`, `GTN_ENGINE_DATA_DIR` and `GTN_ENGINE_STATE_DIR` move the engine's
  configuration, data and state directories without changing `XDG_*` for programs the engine
  starts, such as `uv`. Each names the final directory and must be an absolute path; relative
  values are ignored.
- Node libraries can open a file in the viewer application a user picked in the library's settings
  by sending `LaunchExternalViewerRequest`. The engine starts `<category>.viewer_executable` with
  the arguments in `<category>.viewer_args` followed by the file, and does not wait for it. When
  `fallback_to_os_default` is set and no viewer is chosen, it opens the file with the system's
  default application instead. Requests sent from a worker open the viewer on the user's machine.
  [#4942](https://github.com/griptape-ai/griptape-nodes-engine/issues/4942)
- Variables whose value is a list can now be used in `{VAR}` tokens, for example in a `Text` node,
  and are returned by `ListSubstitutablesRequest`. The list is substituted as text with one item
  per line.
  [#5833](https://github.com/griptape-ai/griptape-nodes-engine/issues/5833)
- Studios can run the engine inside an environment their own tools prepare, listing its libraries in
  the `GTN_LIBRARY_PATHS` environment variable and starting each library's worker inside that
  library's own environment with `worker.command_prefix`. Setting `library.provisioned_by` to
  `environment` makes those the only libraries that load, with nothing downloaded and no virtual
  environments built; `library.sandbox_enabled` lets the sandbox library load in that mode (or turns
  it off in any mode), and `ReloadSandboxLibraryRequest` reloads just the sandbox. See
  [Running in a Managed Environment](https://docs.griptapenodes.com/en/stable/guides/managed_environment/).
- `NodeErrorEvent` has an optional `error` field that holds a node failure in parts: the node's own
  message without the engine's "Attempted to execute node" wrapping or a leading node name, the
  exception type, one message per problem when a node fails validation, and anything the node
  attached with `NodeError`. When the engine wrote the failure itself, such as a worker that stopped
  responding, `error.message` is the engine's text. `ExecuteNodeResultFailure.error` carries the
  same parts back from the process the node ran in. `error_message` is unchanged.
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733)
- Nodes can raise `NodeError` from `griptape_nodes.exe_types.core_types` to attach labelled values
  such as a request ID, the provider's response body, and up to three links, instead of putting
  them in the message text. A link is a web page or, when it starts with `#`, a place in the
  editor, such as the API key the node needs. They reach the editor in `NodeErrorEvent.error`, also
  when the node runs in a worker. See
  [Writing Error Messages](docs/development/custom_nodes/error_handling.md#writing-error-messages).
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733)
- Execution events carry run times. `NodeResolvedEvent` and `NodeErrorEvent` have an optional
  `run_seconds` with how long the node ran, and `ControlFlowResolvedEvent` and
  `ControlFlowCancelledEvent` have one with how long the whole run took, so the editor can show where
  a run spent its time. An End Loop node's or group node's time includes the nodes inside it.
  [#5717](https://github.com/griptape-ai/griptape-nodes-engine/issues/5717)
- `PublicArtifactUrlParameter` has `aget_public_url_for_parameter()` and
  `adelete_uploaded_artifact()`. Nodes using them can upload multiple reference images concurrently
  without blocking other nodes or delaying "Stop". Uploads interrupted by "Stop" finish and are
  deleted in the background.
  [#5729](https://github.com/griptape-ai/griptape-nodes-engine/issues/5729)
- The engine installs and runs on Python 3.14 and 3.15.
  [#5863](https://github.com/griptape-ai/griptape-nodes-engine/issues/5863)
- A saved workflow placed in the sandbox library folder now appears as a node in the Sandbox
  Library, with the workflow's Start Flow and End Flow parameters as its inputs and outputs, so it
  can be reused inside another workflow. `RegisterSandboxNodeFromSourceRequest` accepts a saved
  workflow too. See "Turning a workflow into a node" in the libraries guide.
  [#5657](https://github.com/griptape-ai/griptape-nodes-engine/issues/5657)

### Changed

- **Breaking:** `ControlFlowResolvedEvent` sends a parameter value with no plain-data form as `null`
  instead of as its text, and no longer has `unique_parameter_uuid_to_values`.
- **Breaking:** In `SerializeFlowToCommandsResultSuccess` and
  `ExtractFlowCommandsFromImageMetadataResultSuccess`, each node's `element_modification_commands`
  lists `{"request_type": ..., "request": {...}}` entries, the form `EventRequestBatch` takes,
  instead of each request's fields alone, so each request reads back as its own type.
- **Breaking:** A request or event field typed `Any` that holds a griptape object, or a value JSON
  has no form for, fails to send with an error naming the payload. It used to be sent as the
  object's `to_dict()` or as its text. A result that fails to send this way answers its request
  with a generic failure naming the reason, instead of leaving the request without a reply.
  Annotate a field that carries parameter values `Value` so they send, as described in
  [`get_request_handlers`](docs/development/custom_nodes/advanced_libraries.md#get_request_handlers).
  See [MIGRATION.md](MIGRATION.md#parameter-values-keep-their-type).
- Copied nodes, and the workflow embedded in an exported PNG, are stored as JSON instead of pickle,
  and the embedded workflow is compressed, so exported PNG files are smaller. Nodes copied and PNG
  files exported by earlier versions still paste and load. A PNG exported by this version doesn't
  load its workflow in earlier ones.
- Serializing an event now walks its payload once instead of twice, cutting serialization cost —
  most noticeably on events with large parameter values.
- `ExecuteNodeRequest` results are no longer broadcast to clients by default. The request carries
  a node's run between its flow and the process it runs in, and no client uses the result.
- A failed run now reports `Attempted to run flow '<flow>'. Failed due to: ...` instead of
  `Failed to kick off flow with name <flow>. Exception occurred: ...`, and names the failed node
  once instead of twice. The old wording read as if the run never started.
- `GetParameterValueRequest` handled inside the engine, as from node code or
  `RetainedMode.get_value`, returns the parameter's value itself instead of a plain-data copy, so
  an artifact comes back as the artifact.
- Parameter values recorded in an image's workflow provenance name their type when JSON has none.
- A node in a library running in its own process fails with an error naming the parameter when an
  input or output has no plain-data form, instead of receiving or sending it as text. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that can cross.
- Image, video, audio, and 3D inputs set from a file path now store a macro path such as
  `{inputs}/cat.png` for files in a project directory or the workspace, and the absolute path
  otherwise, instead of copying the file into `staticfiles/` and storing a `localhost` URL.
  Workflows read those files from disk, so headless and published runs no longer need the
  desktop app running, and values saved as `localhost` URLs by earlier versions are read from
  disk too.
- Each engine now keeps its own workflow registry, reached through `engine.workflow_registry`, so
  engines in one process no longer share registered workflows. `WorkflowRegistry` classmethods
  still work and act on the current engine's registry.
- Nodes that upload media to Griptape Cloud for a public URL now resolve the storage bucket on the
  first upload and reuse it for later uploads. Adding such a node to a workflow no longer contacts
  Griptape Cloud.
  [#5729](https://github.com/griptape-ai/griptape-nodes-engine/issues/5729)
- The engine console no longer prints routine messages, such as library dependency installs,
  `Resolving <node>`, `Flow is complete.`, workflow saves, loop iterations, workflow sync, worker
  and session bookkeeping, and agent prompts and tool calls. A failed node is reported once, by the
  run that hit it, instead of at every layer with a full traceback. Set `log_level` to `DEBUG` to
  see the rest.
- `RunArbitraryPythonStringRequest` failures now start with the exception type, such as
  `ZeroDivisionError: division by zero`, instead of `ERROR: division by zero`. This is the text a
  node that runs Python code shows when the code fails.
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733)
- The sandbox library now loads nodes from folders linked into it, and `~` and environment
  variables in `sandbox_library_directory` are expanded. A value that starts with `$` is still read
  as a secret name.
- Workflows saved by this version do not open in earlier versions. Saved workflow files store
  parameter values as readable data instead of pickle, and saving an unchanged workflow writes the
  same file each time, so saved workflows diff cleanly. Workflows saved by earlier versions still
  open.
  [#5441](https://github.com/griptape-ai/griptape-nodes-engine/issues/5441)
- A class in a request field typed `type`, such as `RegisterArtifactProviderRequest.provider_class`,
  is sent as `module:Qualname` instead of `module.Qualname`, the form parameter values name
  classes by.
- A parameter value with no plain-data form is no longer written to saved workflow files, so its
  node runs again when the workflow reopens. It is also left out of copied nodes, exported images,
  and the copy of a loop's or group's nodes that its iterations run, so those copies use the
  parameter's default. Values flowing into the loop from outside it still arrive. A default value
  with no plain-data form on a parameter added to a node is left out the same way. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that are saved, and how to make a class savable.
- Workflow registration at startup, on `RefreshWorkflowRegistryRequest`, and on project switch now
  reads and parses each workflow file's metadata header once instead of twice, and fetches the
  registered library list once per scan instead of once per file. Workspaces holding many workflows
  finish loading sooner.
  [#5770](https://github.com/griptape-ai/griptape-nodes-engine/issues/5770)

### Deprecated

- `pickle_control_flow_result` on `StartFlowRequest`, `StartFlowFromNodeRequest`,
  `StartLocalSubflowRequest`, `SaveWorkflowRequest`, `SaveWorkflowFileFromSerializedFlowRequest`,
  `PublishWorkflowRequest`, and the workflow executors, and the `--pickle-control-flow-result` CLI
  flag, have no effect. Flow results always travel as plain data. They will be removed in a later
  release.
- Nodes copied, and PNG files exported, by earlier versions still paste and load, with a warning,
  but a later release will stop reading them. Export the image again to keep its workflow loadable.
- `safe_unstructure` from `griptape_nodes.retained_mode.events.event_converter` is deprecated and
  will be removed in a later release. Use `encode_value` from `griptape_nodes.serialization.values`
  to turn a parameter value into plain data. It no longer uses a griptape object's `to_dict()`, or
  falls back to a value's text.
- `use_pickling` on `SerializeNodeToCommandsRequest` has no effect. Parameter values are always
  stored as plain data. It will be removed in a later release.

### Removed

- **Breaking:** Events' `json()` and `dict()` raise `TypeError`. `strict_json()`, `failure_json()`
  and `from_dict()` on events, and `to_json()` on payloads, are removed. Use the converters in
  `griptape_nodes.retained_mode.events.converters`: `engine` keeps parameter values' types for
  another engine process, `client` sends them as plain JSON.
  See [MIGRATION.md](MIGRATION.md#events-are-serialized-by-converters).
- **Breaking:** A library's manifest no longer picks where its nodes run. A library that declared
  `SuggestedWorkerMode(WORKER)`, or was set to "Isolated" through `worker_mode_override`, now loads
  into the engine process, and its `pip_dependencies` install into `<library>/.venv` on the engine's
  own import path, where they can conflict with the engine's packages. Declare
  `pip_dependencies_exec` to run its `process` in a worker instead. `WorkerModeCompatibility`,
  `SuggestedWorkerMode`, and `worker_mode_override` still parse but have no effect. See
  [MIGRATION.md](MIGRATION.md#libraries-no-longer-choose-a-worker) for the other removals.
  [#5420](https://github.com/griptape-ai/griptape-nodes-engine/issues/5420)
- **Breaking:** `StartFlowRequest` no longer accepts `wait_for_completion` or `completion_timeout_ms`.
  The request already answers once the run ends, so neither had any effect. Drop them from calls. To
  bound a run, wrap the call in `asyncio.wait_for` and send `CancelFlowRequest` on timeout.

### Fixed

- Directory cleanup now counts files in subfolders toward `max_directory_size_gb` instead of
  failing with a file-not-found error.
  [#5724](https://github.com/griptape-ai/griptape-nodes-engine/issues/5724)
- `DownloadLibraryRequest` now honors a `url@ref` suffix on `git_url`, checking out that branch,
  tag, or commit instead of failing to clone. An explicit `branch_tag_commit` still takes precedence.
- The engine console no longer prints the same error message more than once when a request, a
  node run, a workflow publish, or a cloud file upload or download fails.
- The warning logged when a library's dependencies cannot install under the engine's own versions
  now includes the installer's explanation, naming the requirement that conflicted.
  [#5748](https://github.com/griptape-ai/griptape-nodes-engine/issues/5748)
- Float, bool, and JSON (dict) variables now substitute into `{VAR}` tokens and appear in the
  variable picker. Before, a `{VAR}` token for one of these stayed in the text as typed. A float
  renders as `1.5`, a bool as `true` or `false`, and a dict as compact JSON.
  [#5839](https://github.com/griptape-ai/griptape-nodes-engine/issues/5839)
- The engine now pins `huggingface-hub<2` to prevent dependency conflicts that mark libraries as
  `FLAWED` on fresh installs.
  [#5752](https://github.com/griptape-ai/griptape-nodes-engine/issues/5752)
- Outputs of a node in a library running in its own process reach downstream nodes as the same
  types, instead of as plain dicts. Artifact classes a library defines itself, such as
  `VideoUrlArtifact`, arrive as that class instead of griptape's class of the same name. A value
  whose class belongs to a library the receiving process does not load still arrives as a dict, and
  reaches the next process that does load it unchanged.
  [#4475](https://github.com/griptape-ai/griptape-nodes-engine/issues/4475)
- A node that reports a result with `set_parameter_value` now shows that result when the node runs
  in a library's isolated process, instead of leaving the output empty. A parameter that has an
  output, set while the node is running, now also records the value as a result, and results are what
  travel back from an isolated process. This covers a parameter that is also kept on display, and one
  that declares no modes at all, which is most of the parameters a library writes. Nothing the
  parameter held before is given up: the value is still the parameter's own, so a node that sets one
  mid-run and reads it on the next run, as a randomized seed does, reads what it set. A parameter
  with no output has nowhere to publish, so a value set on it during a run stays in the process that
  set it.
  [#5663](https://github.com/griptape-ai/griptape-nodes-engine/issues/5663)
- Libraries that vendor code as git submodules (such as SAM3 and MiniMax Remover) now get their
  submodules on install, update, and branch or tag switch. Updating them no longer stops with an
  "Uncommitted Changes" prompt that "Overwrite" could not clear. A SAM3 install that already shows
  the prompt needs one "Overwrite".
  [#5784](https://github.com/griptape-ai/griptape-nodes-engine/issues/5784)
- Loading library metadata no longer stalls the engine while it reads each library's git details.
  On Windows, the editor's repeated metadata requests during startup could hold the engine on
  "Starting your engine" for several minutes.
  [#5767](https://github.com/griptape-ai/griptape-nodes-engine/issues/5767)
- A library on a moving tag such as `stable` keeps following that tag when updated, instead of
  switching to `nightly` whenever both tags point at the same commit. The library checkout records
  the tag it follows, and Library Management reports it as the current ref.
  [#5744](https://github.com/griptape-ai/griptape-nodes-engine/issues/5744)
- Updating a library no longer installs a version that needs a newer engine than the one running.
  Before, Library Management could still offer an update it had already found incompatible, and
  installing it left the library unable to load. Checking for updates on an engine too old for
  the newest version now reports the library as up to date instead of showing an error each time.
- `DeserializeFlowFromCommandsRequest` and `SaveWorkflowFileFromSerializedFlowRequest` sent as JSON
  read their nested node, connection, and parameter commands back as commands, instead of as plain
  dicts the request could not use.
- When a node fails to be created because a library raised its own kind of missing-key error with a
  custom message, the error now shows that message instead of only the bare key.
- `UpdateAgentProviderRequest` sent as JSON with only some provider fields set reads back with just
  those fields, instead of failing validation on the ones left out.
  [#5439](https://github.com/griptape-ai/griptape-nodes-engine/issues/5439)
- A workflow embedded in a PNG loads in a later session even when a node holds a value whose class
  a node library defines, such as the library's own enum or artifact. PNG files exported by earlier
  versions with such values load too, instead of failing.
- Saving an image no longer floods the editor with engine bookkeeping: element events for hidden
  copies of every node, and results of the engine's own path and project lookups. A 72-node image
  workflow sent 66 MB over the websocket per run, and now sends 6 MB.
  [#2823](https://github.com/griptape-ai/griptape-vsl-gui/issues/2823)
- Renaming a node or flow to a name that is already taken now names the requested name in its
  warning, instead of printing `{request.requested_name}`.
- Saved workflows in the sandbox library folder are no longer run when the engine starts. Before,
  a workflow saved in an older format could make itself the workflow the editor opened on every
  launch.
  [#5656](https://github.com/griptape-ai/griptape-nodes-engine/issues/5656)
- `ScanSequencesResultFailure`, `ListDirectoryResultFailure`, `ListDirectorySequencesResultFailure`,
  and `DeduceSequencesFromFileListResultFailure` read back from JSON with their `failure_reason`,
  instead of failing because it may come from either of two enums.
  [#5438](https://github.com/griptape-ai/griptape-nodes-engine/issues/5438)
- Saving a workflow with a subflow node group no longer warns about values the group passes
  through from parameters marked `serializable=False`. `AddParameterToNodeRequest` takes a
  `serializable` field so a parameter added this way keeps that setting when the workflow reopens.
  [#5073](https://github.com/griptape-ai/griptape-nodes-engine/issues/5073)
- Loop iterations that run in a separate process receive artifact and other non-JSON inputs as
  the same types, instead of as plain dicts.
- A node group set to "Private Execution" or to a library execution environment now fails right
  away with a message naming the problem when no `GT_CLOUD_API_KEY` is set, instead of failing
  after a rejected or timed-out connection.
  [#5793](https://github.com/griptape-ai/griptape-nodes-engine/issues/5793)
- A loop or subflow run in a separate process no longer hands back one parameter's value in place
  of another's when one is `True` and the other is `1`.
  [#5435](https://github.com/griptape-ai/griptape-nodes-engine/issues/5435)
- Changing a library's branch or tag in "Library Management" now works on a library that failed to
  load, so a library built for a newer engine can be switched back to a version this engine can run.
  Previously the only libraries that could be switched or updated were the ones already loaded.
- A request with a field typed `type`, such as `RegisterArtifactProviderRequest`, reads back from
  JSON into the class it names, instead of failing.
  [#5437](https://github.com/griptape-ai/griptape-nodes-engine/issues/5437)
- The "Conflicting values" warning for a parameter's `ui_options` now names the node, its type, and
  the library that defined it, so it is clear which library author to contact.
  [#5742](https://github.com/griptape-ai/griptape-nodes-engine/issues/5742)
- A library copied into `libraries_directory` without being registered no longer goes missing
  silently. Discovery logs the manifest's path and how to register it, and the
  `libraries_directory` description no longer claims libraries there load on startup: a library
  loads only from `libraries_to_register` (or Add Library) or a `libraries_to_download` entry.
  [#5749](https://github.com/griptape-ai/griptape-nodes-engine/issues/5749)
- Workflows created with `push_workflow(workflow_name=...)` no longer flood the logs with
  "Optional builtin 'workflow_dir' could not be resolved" warnings before their first save.
  `workflow_dir` now uses the default save folder when no working directory was provided.
- A workflow used by a workflow node no longer appears twice in the workflow list when it is inside
  the workspace.

### Security

- A request that fails outside its handler is now logged by type and request ID only. The engine
  used to log the whole request, so a failed `SetSecretValueRequest` wrote the secret value to the
  engine log in plain text.
  [#5739](https://github.com/griptape-ai/griptape-nodes-engine/issues/5739)
- Loading a workflow from a PNG, and pasting nodes, no longer unpickle the data unrestricted, no
  longer run a command in a place serialization never writes that type of command, and no longer
  import a saved control's module from outside Griptape Nodes and its libraries. Each gap let a
  crafted image or clipboard payload run any command or module when loaded. Data from earlier
  versions is read by a reader that builds only saved value types and applies the same checks.

## [0.103.0] - 2026-09-29

### Changed

- **Breaking:** The setting `worker.heartbeat_startup_grace_s` is now `worker.library_load_timeout_s`
  (env `GTN_CONFIG_WORKER__LIBRARY_LOAD_TIMEOUT_S`). With its heartbeat role removed, what it bounds
  is how long a worker may take to load its library, which the new name states. A config file still
  setting the old name silently falls back to the 600 second default.
- Workflows run in a subprocess now verify TLS certificates against the operating system's trust
  store, matching the app.

### Removed

- **Breaking:** The engine no longer patches `httpx`, `httpx2`, and `requests` to read `file://` URLs,
  local paths, and cloud asset URLs in workflows run or published in a subprocess. Nodes that fetched
  those through `httpx`, `httpx2`, or `requests` must read the file directly instead.

### Fixed

- A `Workflow Node` now starts each parameter it exposes from a workflow's `Start Flow` node with
  the value set on that `Start Flow` node, instead of leaving it empty. The value is saved with the
  workflow, so a workflow saved before this release needs saving again to carry it. Values such as
  images keep the parameter's own default.
  [#5698](https://github.com/griptape-ai/griptape-nodes-engine/issues/5698)
- The process a library runs isolated in shuts down within about 35 seconds of losing the engine
  that started it. Before, if that engine exited in the process's first 10 minutes, the process
  stayed up until those 10 minutes had passed. `worker.library_load_timeout_s` no longer delays
  that check; it still bounds how long the engine waits for the process to load its library.
  Setting `worker.heartbeat_timeout_s` below 30 seconds does not shorten this, on purpose: a busy
  engine can be slow to challenge, and a library's process must not read that as an engine that died.
- A library whose isolated process shuts down before loading it now reports that as soon as the
  process goes, instead of waiting out `worker.library_load_timeout_s` and then blaming a library
  load that never finished.
- Installing a library's dependencies no longer gives the engine an older copy of a package the
  engine itself imports. A library's environment comes ahead of the engine's own on the import path,
  so a library that resolved, for instance, an older `griptape` handed that copy to the engine too.
  Library installs now carry the engine's own versions as minimum versions, so such a package
  resolves no older than the engine's. A library that genuinely needs an older one is still
  installed and still works; it is now listed in that library's problems, naming what it supplies
  and what the engine expected, where before nothing connected the two.
  [#5681](https://github.com/griptape-ai/griptape-nodes-engine/issues/5681)
  [#5682](https://github.com/griptape-ai/griptape-nodes-engine/issues/5682)
- Creating or switching to a project whose workspace differs now closes the open workflow, returning
  you to the workflow picker. Before, the engine kept a workflow it no longer had a record of, so the
  next workflow you opened sat on "Checking workflow" and the log filled with "is not registered on
  this engine" warnings until you restarted the engine.
  [#5692](https://github.com/griptape-ai/griptape-nodes-engine/issues/5692)
- Saving a file from a workflow you have not saved yet no longer logs a stream of "Optional builtin
  'workflow_dir' could not be resolved" warnings. `workflow_dir` now answers with the folder your
  first save would default to, read from the project's `save_workflow` situation, so a project that
  points workflow saves outside the workspace root writes those files there rather than at the root.
  [#5669](https://github.com/griptape-ai/griptape-nodes-engine/issues/5669)
- Creating a versioned output folder or file sequence in a project no longer fails with "requires
  at most one unresolved variable" when its path uses a project directory such as `{outputs}`.
  `GetNextVersionIndexRequest` now fills in project directories and built-in variables itself, so
  callers only supply their own variables.
- A parameter that a node both shows and passes on, such as the text on a text node, keeps an edit
  made after the node has run. Before, reopening the workflow or refreshing the page showed the
  value from the last run instead of the edit.
- Renaming a parameter that holds an output value now reports that the old name no longer has one,
  alongside the new name's value. Before, only the new name was reported, so anything tracking
  output values by parameter name kept the old name's value.
- Saving HEIC, AVIF, or ICO bytes no longer rewrites the destination's extension to match the
  detected format, and no longer fails when `coerce_extension_to_match_bytes` is off. The engine
  does not recognize these formats, so the file is written at the extension you asked for and a
  warning is logged.
  [#5614](https://github.com/griptape-ai/griptape-nodes-engine/issues/5614)

### Added

- `claude-sonnet-5-5` is available in Griptape Cloud model dropdowns and the chat sidebar.
- Projects have two new situations for versioned output folders. `save_output_directory` creates
  `{outputs}/renders_v001`, then `renders_v002` on the next run. `save_file_sequence` writes each
  run's frames into a new version folder, such as `frames_v001/frames.0001.png`. Node libraries
  use them through `ProjectDirectoryParameter` and `ProjectFileSequenceParameter`. Projects on
  the legacy template fall back to the same layout. See
  [Situations](https://docs.griptapenodes.com/en/stable/guides/projects/situations/#save_output_directory).

## [0.102.0] - 2026-09-24

### Added

- Libraries can list heavy packages under `pip_dependencies_exec` in their manifest. Those install
  into a separate `.venv-exec` and load only in the library's own process, where its nodes run, so
  libraries with clashing heavy pins can be installed side by side.
- A node in a library that runs isolated in a worker can hand an unserializable value, such as a
  diffusers pipeline or a latent tensor, to the next node. Mark the producing output
  `serializable=False` and the engine holds the object in the worker, sending an opaque key in its
  place that the consuming node's read resolves. See
  [MIGRATION.md](MIGRATION.md#serializablefalse-outputs-are-held-in-their-own-process-across-a-worker-boundary).
- Claude Opus 5.5, GPT-6 Sol, and GPT-6 Luna are in the model catalog.
- Nodes can implement `validate_in_execution_environment()` to run a check where the node itself runs,
  which for a library isolated in its own process is where its heavy packages are importable and its
  inputs are the real objects. A node that fails the check reports why in
  `ExecuteNodeResultFailure.validation_exceptions` instead of crashing partway through.
- You can try new features early by turning them on from the **Beta Features** page in the
  editor's settings, and turn them off again at any time. Node libraries can offer beta features
  of their own. See
  [Beta Features](https://docs.griptapenodes.com/en/stable/guides/editor/beta_features/), and
  [Authoring Libraries](https://docs.griptapenodes.com/en/stable/development/custom_nodes/authoring_libraries/#beta-features)
  to add them to a library.
- The `Slider` trait, and `ParameterInt` and `ParameterFloat` with `slider=True`, take
  `soft_limits=True`. The slider then spans its range, but a value typed outside it is accepted
  instead of rejected, matching soft limits in Nuke, Maya, and Houdini.
  [#5269](https://github.com/griptape-ai/griptape-nodes-engine/issues/5269)
- Custom traits can keep settings a node changes at runtime, such as a narrowed range, when the
  workflow is saved and reopened, by implementing `to_state()` and `apply_state()`. See
  [MIGRATION.md](MIGRATION.md#traits-can-save-runtime-state).

### Changed

- **Breaking:** `WorkflowPackager.package_to_folder` returns a `PackagedBundle` with the bundled
  workflow's path and the library paths, instead of a list of library paths. See
  [MIGRATION.md](MIGRATION.md#package_to_folder-reports-where-it-put-the-workflow).
  [#5326](https://github.com/griptape-ai/griptape-nodes-engine/issues/5326)
- An app event raised in one process is no longer delivered to listeners in another. A library running
  isolated in its own process reports to the engine by sending a request instead.
- **Breaking:** When a parameter's `ui_options` and a custom trait set the same key, the trait's
  value now wins, so node code can no longer override a trait's widget settings through
  `ui_options`. Implement `state_from_ui_options()` on the trait to accept these overrides, or
  change the trait's own attributes instead.
- Setting a value outside a `Slider` range now fails with an error naming the parameter, the value,
  and the allowed range, instead of "Value out of range".
  [#5269](https://github.com/griptape-ai/griptape-nodes-engine/issues/5269)

### Removed

- **Breaking:** `LibraryLoadedNotification` no longer carries `node_schemas`. A library that loaded in
  its own process reports its schemas to the engine with the new `ReportLibraryLoadedRequest`, and the
  notification that follows says only how the load went.
- `TraitRegistry` and `Trait.get_trait_keys()` are removed, with no replacement, since nothing read
  them. Custom traits no longer need to implement `get_trait_keys()`, and existing implementations
  can be deleted.
- The engine no longer runs its own static file server. The Griptape Nodes app serves the
  workspace, as it has since v0.95.0. `STATIC_SERVER_ENABLED` is gone.

### Fixed

- Connecting a video, image, or audio file uploaded through the editor to a node that requires that
  media type no longer fails with a message saying the parameter must be an artifact.
- Image, video, audio, and 3D parameters no longer fail when given an inline `data:` URI longer than
  the operating system's file name limit, which any real image exceeds. The URI is kept as the
  parameter's value.
- Image, video, audio, and 3D inputs given a file path use the file where it already is, instead of
  copying it into `staticfiles/`. The copy could overwrite a different file with the same name. A
  saved workflow now depends on its input files staying put: moving, renaming, or deleting one
  breaks the workflow, and an input outside the workspace is referenced by its absolute path, so the
  project is no longer portable across machines for those inputs.
  [#5647](https://github.com/griptape-ai/griptape-nodes-engine/issues/5647)
- Model dropdowns no longer mark every model "Not permitted by your license" when two installed
  libraries provide a node with the same name.
  [#5618](https://github.com/griptape-ai/griptape-nodes-engine/issues/5618)
- Group nodes in a reopened workflow show the ports and connections of parameters added to the
  group again.
  [#5563](https://github.com/griptape-ai/griptape-nodes-engine/issues/5563)
- A node can be deleted while a workflow is running. Deleting a node the run still needs cancels the
  run; deleting any other node lets it finish.
- Image previews no longer break when several requests regenerate the same preview at once, and
  synced or copied images are no longer treated as changed on every view. When a preview cannot be
  made, the engine reports why.
- Workflows created from a template or by branching are saved where the project saves workflows,
  instead of always in the workspace folder. Branching twice no longer overwrites the first branch.
- Packaging a workflow stops with an error when the workflow or one of its files has the same name
  as a file the bundle reserves.
  [#5323](https://github.com/griptape-ai/griptape-nodes-engine/issues/5323)
- `RunWorkflowWithCurrentStateRequest` fails when a workflow is already open, instead of attaching
  the target as a hidden flow that was saved and run along with the open workflow.
  [#5526](https://github.com/griptape-ai/griptape-nodes-engine/issues/5526)
- A slider range, dropdown choices, or button link that a node changes at runtime now survives
  saving and reopening the workflow. Before, the reopened workflow showed the saved settings, but
  sliders checked the old range, dropdowns the old choices, and buttons opened the old link.
  [#5440](https://github.com/griptape-ai/griptape-nodes-engine/issues/5440)
- Changing a slider's range or a dropdown's choices through `ui_options`, from node code or the
  editor, now also changes which values the parameter accepts. Before, the editor showed the new
  range or choices, but the parameter still checked values against the old ones.
  [#5440](https://github.com/griptape-ai/griptape-nodes-engine/issues/5440)
- Nodes in a library that runs isolated in its own process no longer stop working mid-session while
  the engine is busy. That process is now dropped for leaving heartbeat challenges unanswered rather
  than for elapsed time, so `worker.heartbeat_timeout_s` bounds unanswered challenges instead of
  wall-clock silence.
- A node that writes a list or dictionary to an output and reads it back gets the same object rather
  than a copy of it, and a value that refers to itself no longer fails the node with a
  `RecursionError`. Inline `{VAR}` substitution returns a value it did not rewrite unchanged.

[Unreleased]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.105.0...HEAD
[0.105.0]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.104.2...v0.105.0
[0.104.2]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.104.1...v0.104.2
[0.104.1]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.104.0...v0.104.1
[0.104.0]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.103.0...v0.104.0
[0.103.0]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.102.0...v0.103.0
[0.102.0]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.101.0...v0.102.0
