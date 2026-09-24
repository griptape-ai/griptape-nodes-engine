# Changelog

All notable changes to Griptape Nodes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
**Breaking** marks a change that can stop a saved workflow, a node library, or a client of
the engine's request API from working without edits. Migration steps live in
[MIGRATION.md](MIGRATION.md).

## [Unreleased]

### Added

- Libraries can list heavy packages under `pip_dependencies_exec` in their manifest. Those install
  into a separate `.venv-exec` and load only in the library's own process, where its nodes run, so
  libraries with clashing heavy pins can be installed side by side.
- Claude Opus 5.5, GPT-6 Sol, and GPT-6 Luna are in the model catalog.
- `Slider`, and `ParameterInt` / `ParameterFloat` with `slider=True`, take `soft_limits=True` to
  accept a typed value outside the slider's range instead of rejecting it. The range then only sizes
  the slider track.
  [#5269](https://github.com/griptape-ai/griptape-nodes-engine/issues/5269)
- Custom traits can save runtime changes by implementing `to_state()` and `apply_state()`. A saved
  trait the node does not build is rebuilt on load with `from_state()`. See
  [MIGRATION.md](MIGRATION.md#traits-can-save-runtime-state).

### Changed

- **Breaking:** `WorkflowPackager.package_to_folder` returns a `PackagedBundle` with the bundled
  workflow's path and the library paths, instead of a list of library paths. See
  [MIGRATION.md](MIGRATION.md#package_to_folder-reports-where-it-put-the-workflow).
  [#5326](https://github.com/griptape-ai/griptape-nodes-engine/issues/5326)
- A `ui_options` key a trait renders, such as `slider` or `simple_dropdown`, now always holds the
  trait's value. Writing that key updates the trait, and the value is saved as trait state instead
  of in `ui_options`. A custom trait accepts these writes by implementing `state_from_ui_options()`;
  otherwise the write is ignored with a warning.
- `AlterParameterDetailsRequest.traits` takes a list of saved trait states instead of a set of trait
  names, which it ignored. `AddParameterToNodeRequest` takes the same `traits` list.
- Setting a value outside a slider's range fails with a message naming the parameter, the value,
  and the range, instead of "Value out of range".

### Removed

- `TraitRegistry`, `traits.json`, and `Trait.get_trait_keys()` are gone, since nothing read them.
  Custom traits no longer need to implement `get_trait_keys()`; existing implementations are
  ignored.

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
- Slider ranges, dropdown choices, button links, and other trait settings a node changes after
  building a parameter now survive saving and reopening the workflow, including on parameters the
  node added itself. Before, the editor showed the saved settings but the parameter did not act on
  them, so a narrowed slider still accepted values outside its range.
  [#5440](https://github.com/griptape-ai/griptape-nodes-engine/issues/5440)

[Unreleased]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.101.0...HEAD
