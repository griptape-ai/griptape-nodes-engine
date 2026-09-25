# Changelog

All notable changes to Griptape Nodes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
**Breaking** marks a change that can stop a saved workflow, a node library, or a client of
the engine's request API from working without edits. Migration steps live in
[MIGRATION.md](MIGRATION.md).

## [Unreleased]

### Changed

- **Breaking:** Parameter values the engine sends to the editor and to request API clients name
  their type when JSON has no such type. A tuple arrives as
  `{"$type": "builtins:tuple", "$value": [1, 2]}` instead of a list, an enum as its value under
  `$value` instead of the bare value, and an artifact with a `$type` key beside its fields. This
  covers `ParameterValueUpdateEvent`, `NodeResolvedEvent`, `AlterElementEvent`, and the results of
  `GetParameterValueRequest`, `SetParameterValueRequest`, `GetParameterDetailsRequest`,
  `GetNodeElementDetailsRequest`, `GetAllNodeInfoRequest`, and `RunArbitraryPythonStringRequest`
  (its `found_variable_values`). Flow variable values cross the same way, in `CreateVariableRequest`,
  `SetVariableValueRequest`, and the results of `GetVariableRequest`, `GetVariableValueRequest`,
  `GetVariablesRequest`, and `ListVariablesRequest`. A value sent in the same form to
  `SetParameterValueRequest` or `SetVariableValueRequest` is set with that exact type. See
  [Parameter values](docs/guides/mcp/external_clients.md#parameter-values).
- **Breaking:** In `SerializeFlowToCommandsResultSuccess` and
  `ExtractFlowCommandsFromImageMetadataResultSuccess`, each node's `element_modification_commands`
  lists `{"request_type": ..., "request": {...}}` entries, the form `EventRequestBatch` takes,
  instead of each request's fields alone, so each request reads back as its own type.
- **Breaking:** A request or event field typed `Any` that holds a griptape object, or a value JSON
  has no form for, fails to send with an error naming the payload. It used to be sent as the
  object's `to_dict()` or as its text. Fields that carry parameter values send them tagged with
  their type instead; see the first entry.
- Saved workflow files store parameter values as readable data instead of pickle, and saving an
  unchanged workflow writes the same file each time, so saved workflows diff cleanly. Workflows saved
  by earlier versions still open. A workflow saved by this version does not open in earlier ones.
  [#5441](https://github.com/griptape-ai/griptape-nodes-engine/issues/5441)
- A parameter value with no plain-data form is no longer written to saved workflow files, so its
  node runs again when the workflow reopens. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that are saved, and how to make a class savable.
- A node in a library running in its own process fails with an error naming the parameter when an
  input or output has no plain-data form, instead of receiving or sending it as text. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that can cross.
- `GetParameterValueRequest` handled inside the engine, as from node code or
  `RetainedMode.get_value`, returns the parameter's value itself instead of a plain-data copy, so
  an artifact comes back as the artifact.
- Parameter values recorded in an image's workflow provenance name their type when JSON has none,
  the same way the request API sends them.
- A class in a request field typed `type`, such as `RegisterArtifactProviderRequest.provider_class`,
  is sent as `module:Qualname` instead of `module.Qualname`, the form parameter values name
  classes by.
- `ExecuteNodeRequest` results are no longer broadcast to clients by default. The request carries
  a node's run between its flow and the process it runs in, and no client uses the result.
- Copied nodes, and the workflow embedded in an exported PNG, are stored as JSON instead of pickle,
  and the embedded workflow is compressed, so exported PNG files are smaller. Nodes copied and PNG
  files exported by earlier versions still paste and load. A PNG exported by this version doesn't
  load its workflow in earlier ones.

### Deprecated

- `pickle_control_flow_result` on `StartFlowRequest`, `StartFlowFromNodeRequest`,
  `StartLocalSubflowRequest`, `SaveWorkflowRequest`, `SaveWorkflowFileFromSerializedFlowRequest`,
  `PublishWorkflowRequest`, and the workflow executors, and the `--pickle-control-flow-result` CLI
  flag, have no effect. Flow results always travel as plain data. They will be removed in a later
  release.
- `safe_unstructure` from `griptape_nodes.retained_mode.events.event_converter` is deprecated and
  will be removed in a later release. Use `encode_value` from `griptape_nodes.serialization.values`
  to turn a parameter value into plain data. It no longer uses a griptape object's `to_dict()`, or
  falls back to a value's text.
- Nodes copied, and PNG files exported, by earlier versions still paste and load, with a warning,
  but a later release will stop reading them. Export the image again to keep its workflow loadable.

### Fixed

- `DeserializeFlowFromCommandsRequest` and `SaveWorkflowFileFromSerializedFlowRequest` sent as JSON
  read their nested node, connection, and parameter commands back as commands, instead of as plain
  dicts the request could not use.
- Loop iterations that run in a separate process receive artifact and other non-JSON inputs as
  the same types, instead of as plain dicts.
- A loop or subflow run in a separate process no longer hands back one parameter's value in place
  of another's when one is `True` and the other is `1`.
  [#5435](https://github.com/griptape-ai/griptape-nodes-engine/issues/5435)
- Outputs of a node in a library running in its own process reach downstream nodes as the same
  types, instead of as plain dicts. Artifact classes a library defines itself, such as
  `VideoUrlArtifact`, arrive as that class instead of griptape's class of the same name. A value
  whose class belongs to a library the receiving process does not load still arrives as a dict, and
  reaches the next process that does load it unchanged.
  [#4475](https://github.com/griptape-ai/griptape-nodes-engine/issues/4475)
- A request with a field typed `type`, such as `RegisterArtifactProviderRequest`, reads back from
  JSON into the class it names, instead of failing.
  [#5437](https://github.com/griptape-ai/griptape-nodes-engine/issues/5437)
- `ScanSequencesResultFailure`, `ListDirectoryResultFailure`, `ListDirectorySequencesResultFailure`,
  and `DeduceSequencesFromFileListResultFailure` read back from JSON with their `failure_reason`,
  instead of failing because it may come from either of two enums.
  [#5438](https://github.com/griptape-ai/griptape-nodes-engine/issues/5438)
- `UpdateAgentProviderRequest` sent as JSON with only some provider fields set reads back with just
  those fields, instead of failing validation on the ones left out.
  [#5439](https://github.com/griptape-ai/griptape-nodes-engine/issues/5439)
- A workflow embedded in a PNG loads in a later session even when a node holds a value whose class
  a node library defines, such as the library's own enum or artifact. PNG files exported by earlier
  versions with such values load too, instead of failing.

### Security

- Loading a workflow from a PNG, and pasting nodes, no longer unpickle the data unrestricted, which
  let a crafted image run any command when loaded. Data from earlier versions is read by a reader
  that builds only saved value types.

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

[Unreleased]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.102.0...HEAD
[0.102.0]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.101.0...v0.102.0
