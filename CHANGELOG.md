# Changelog

All notable changes to Griptape Nodes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
**Breaking** marks a change that can stop a saved workflow, a node library, or a client of
the engine's request API from working without edits. Migration steps live in
[MIGRATION.md](MIGRATION.md).

## [Unreleased]

### Changed

- A node in a library running in its own process fails with an error naming the parameter when an
  input or output has no plain-data form, instead of receiving or sending it as text. See
  [Parameter values](docs/development/custom_nodes/parameters.md#parameter-values) for the types
  that can cross.

### Deprecated

- `pickle_control_flow_result` on `StartFlowRequest`, `StartFlowFromNodeRequest`,
  `StartLocalSubflowRequest`, `SaveWorkflowRequest`, `SaveWorkflowFileFromSerializedFlowRequest`,
  `PublishWorkflowRequest`, and the workflow executors, and the `--pickle-control-flow-result` CLI
  flag, have no effect. Flow results always travel as plain data. They will be removed in a later
  release.

### Fixed

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
