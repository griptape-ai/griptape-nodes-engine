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
- A node in a library that runs isolated in a worker can hand an unserializable value, such as a
  diffusers pipeline or a latent tensor, to the next node. Mark the producing output
  `serializable=False` and the engine holds the object in the worker, sending an opaque key in its
  place that the consuming node's read resolves. See
  [MIGRATION.md](MIGRATION.md#serializablefalse-outputs-are-held-in-their-own-process-across-a-worker-boundary).
- Claude Opus 5.5, GPT-6 Sol, and GPT-6 Luna are in the model catalog.

### Changed

- **Breaking:** `WorkflowPackager.package_to_folder` returns a `PackagedBundle` with the bundled
  workflow's path and the library paths, instead of a list of library paths. See
  [MIGRATION.md](MIGRATION.md#package_to_folder-reports-where-it-put-the-workflow).
  [#5326](https://github.com/griptape-ai/griptape-nodes-engine/issues/5326)
- An app event raised in one process is no longer delivered to listeners in another. A library running
  isolated in its own process reports to the engine by sending a request instead.

### Removed

- **Breaking:** `LibraryLoadedNotification` no longer carries `node_schemas`. A library that loaded in
  its own process reports its schemas to the engine with the new `ReportLibraryLoadedRequest`, and the
  notification that follows says only how the load went.

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
- Nodes in a library that runs isolated in its own process no longer stop working mid-session while
  the engine is busy. That process is now dropped for leaving heartbeat challenges unanswered rather
  than for elapsed time, so `worker.heartbeat_timeout_s` bounds unanswered challenges instead of
  wall-clock silence.
- A node that writes a list or dictionary to an output and reads it back gets the same object rather
  than a copy of it, and a value that refers to itself no longer fails the node with a
  `RecursionError`. Inline `{VAR}` substitution returns a value it did not rewrite unchanged.

[Unreleased]: https://github.com/griptape-ai/griptape-nodes-engine/compare/v0.101.0...HEAD
