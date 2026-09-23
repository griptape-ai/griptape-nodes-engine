# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development

**Commands**

All development commands use the Makefile:

```bash
make check # check linting/formatting/type errors
make fix # fix autofixable errors
```

**Iteration Loop**

When developing, follow this iteration loop:

1. **Make the change**: make the changes required to implement a feature or fix a bug
1. **Run checks**: run `make check` (or `make fix`) to see if any linting/formatting/type errors arose
1. **Fix issues**: resolve all issues from previous step
1. **Continue working**: continue to the next change

## Code Style Preferences

**Avoid Tuples For Return Values** - Tuples should be a last resort. When unavoidable, use NamedTuples for clarity. Prefer separate variables, class instances, or other data structures.

**Simple, Readable Logic Flow** - Prefer simple, easy-to-follow logic over complex nested expressions. Use explicit if/else statements instead of ternary operators or nested conditionals. Break complex nested expressions into clear, separate statements.

**Evaluate ALL failure cases first, success path ONLY at the end** - ALL validation checks, error conditions, and failure cases must be at the top of the function. Each failure case should exit immediately (return/raise). The success path must be at the absolute bottom of the function.

**Do NOT use lazy imports** - All imports must be at the top of the file. Never import inside functions unless it is the only way to resolve a circular import. If a lazy import is required, add a comment explaining which circular dependency makes it necessary.

**Class organization order** - Organize class members in this order:

1. Class attributes
1. `__init__`
1. Other dunder methods
1. Properties
1. Public instance methods
1. Private instance methods
1. Class methods
1. Static methods

Instance methods come first because they can call anything. Class methods come next because they can only call class/static methods. Static methods come last because they can't call other class methods. Within each group, put high-level methods first and helper methods below the callers that use them.

## Exception Handling

**Only wrap code that actually raises exceptions** - Verify that code raises exceptions before adding try/except. Do not add try/except blocks speculatively. If unsure, ask first.

**Use specific, narrow exception blocks** - Catch only the specific exception types that can be raised. Keep try blocks as small as possible — wrap only the exact lines that raise. Never use bare `except:` or catch `Exception` unless explicitly required.

**Write artist-comprehensible, user-facing error messages** - User-facing error messages must be understandable by artists, not just engineers. Avoid stack-trace jargon, internal type names, and implementation details. Use the format: "Attempted to do X to Y. Failed due to Z." Include `{self.name}` when available. Include relevant parameter names and operation context.

## Path Handling

**Canonicalize at the boundary, not in the middle** - The OS boundary (`OSManager.on_write_file_request`, `LocalFileDriver._resolve_path`) already canonicalizes incoming paths. Do not wrap a path with `canonicalize_for_io` before passing it to `ReadFileRequest` / `WriteFileRequest` or to a `FileDriver` method, it is redundant.

**Use `canonicalize_for_identity` for keys** - When a path is about to become a dict key, cache key, dedupe-set member, or workspace-containment input, call `canonicalize_for_identity(path)` from `griptape_nodes.files.path_utils`. It sanitizes + expands `~`/env vars + absolutizes + follows symlinks, so two spellings of the same file collide. Prefer it over ad-hoc `Path(x).resolve()`, which skips `expanduser` and causes identity drift.

**Use `canonicalize_for_io` for OS-level I/O** - Reach for `canonicalize_for_io(path)` only when handing a path directly to the OS (inside a handler or driver, or calling `open()`/`os.*` yourself). It does the same work as the identity variant without following symlinks and adds the Windows long-path prefix when needed.

**Prefer the named helpers over composing primitives** - `sanitize_path_string`, `expand_path`, `resolve_path_safely`, and `normalize_path_for_platform` are building blocks. If you find yourself chaining them, use one of the two canonicalize helpers instead so behavior stays consistent across call sites.

## Media Parameter Values

**Reduce a media value to a string, then normalize it** - A media parameter value arrives as an artifact, a string (URL, project macro path, filesystem path), or a serialized artifact dict. `normalize_artifact_input` handles all three by collapsing them to a string and handing it to `_normalize_string_input`, which resolves the path, uploads it to static storage, and builds the artifact type the parameter declared. Add new input shapes by extracting their string and falling through to that branch. Do not rebuild the artifact from its marshmallow schema (`BaseArtifact.from_dict` / `get_schema().load()`): generated schemas set `unknown = INCLUDE`, so the display metadata the editor sends alongside a value (`width`, `height`, `duration`) reaches the constructor and raises, and the schema builds whatever type the dict names rather than the type the parameter wants. The string route has neither problem.

**A serialized dict's declared `type` distinguishes a path from a payload** - `<Kind>UrlArtifact` dicts hold a path or URL in `value`; raw `<Kind>Artifact` dicts hold base64 bytes. Only unwrap `value` when the dict's `type` names the artifact type you are normalizing to, or base64 is treated as a path. The node libraries make the same check in `coerce_media_url_or_data_uri`.

**Do not derive artifact ids to stabilize equality** - `BaseArtifact.id` defaults to random hex and takes part in `__eq__`, and `NodeManager` reads artifact inequality as a user edit, so normalizing the same input twice unresolves the downstream subgraph. That is a property of the existing string branch too, and the fix belongs in that comparison, not in the value feeding it. Do not fingerprint payloads into synthetic ids to work around it for one branch — it leaves the branch beside it inconsistent and hides the real bug. See [#5621](https://github.com/griptape-ai/griptape-nodes-engine/issues/5621).

## Documentation

**Update docs with user-facing changes** - When a change affects what users see or do, update the documentation in the same PR. Common mappings:

- New or changed CLI commands/flags → `docs/reference/command_line_interface.md`
- New or changed settings → `docs/reference/configuration_reference.md` (and `docs/guides/configuration.md` if it needs explanation)
- New user-facing features or request event families → the relevant page under `docs/guides/`, or a new page
- Editor-facing behavior changes (shortcuts, menus, panels) → `docs/guides/editor/`
- Deprecated or renamed nodes → `MIGRATION.md` and the node's doc page

**Wire new pages into mkdocs.yml twice** - A new docs page must be added to both the `nav` section and the `llmstxt` plugin sections in `mkdocs.yml`. Verify with `uv run mkdocs build --strict`.

**Write for artists** - Docs follow the same rule as error messages: understandable by artists, not just engineers. Use exact UI labels, menu paths, and shortcuts. Match the voice of existing pages such as `docs/guides/libraries.md`.

## Changelog

`CHANGELOG.md` follows [Keep a Changelog 2.0.0](https://keepachangelog.com/en/2.0.0/). It is the record people read to learn what changed between versions, and each version's section is its GitHub release notes. Its readers are artists using the editor, node library authors, and clients of the request API.

**Add an entry for every user-facing change** - In the same PR, add a bullet under `## [Unreleased]`. User-facing means anything a user notices after upgrading: node behavior, editor-visible behavior, saved workflow files, the node library API (`exe_types/core_types.py`, `GriptapeNodes`), request/response events, settings, CLI commands, and supported platforms or Python versions. After adding one, tell the user so they can review the wording. Machines draft, humans curate.

**Skip what users never see** - Refactors, tests, CI, comments, docs-only changes, and dev dependency bumps get no entry. Neither does a fix for a bug that never shipped in a release: edit or delete the entry that introduced it. A runtime dependency bump gets an entry only if users feel it, and the entry describes that effect, not the bump.

**Pick one of the six types** - Use `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, or `### Security`, in that order, adding the heading if the section lacks it. No other headings; `make check/changelog` rejects them.

- `Added`: a new capability.
- `Changed`: the old behavior was intentional and now differs. Performance work goes here.
- `Fixed`: the old behavior was a bug. Unsure between `Fixed` and `Changed`? Ask whether the old behavior was a bug.
- `Deprecated`: still works, will be removed. Name the replacement and the version that removes it.
- `Removed`: gone. Name the replacement.
- `Security`: fixes a vulnerability. Lead with the CVE ID if there is one.

`feat:` is usually `Added` or `Changed`, `fix:` is `Fixed`, `perf:` is `Changed`.

**Describe the result, not the code** - Write what the user sees after upgrading, for a reader with zero context about the PR:

- Say where and when: the node, panel, request, setting, or situation it affects.
- Present tense, subject first: "X now ...", "X no longer ...".
- Use exact names in backticks for nodes, parameters, settings, requests, and CLI flags, and exact UI labels in quotes. Internal classes and functions only if the reader calls them.
- Explain why when it is not obvious.
- One or two sentences. Longer explanations go in docs or `MIGRATION.md`, linked from the entry.
- Plain words. No "improved", "enhanced", or "better"; say what changed.
- One entry per change. When a later PR extends an unreleased change, edit its entry instead of adding another.
- Link the GitHub issue on its own last line when there is one: `[#1234](https://github.com/griptape-ai/griptape-nodes-engine/issues/1234)`. Issues carry the background and lead on to the PRs. Leave out PR numbers, commit hashes, and `@handles`; they record the implementation, not why it changed.
- Wrap lines at about 100 characters and indent continuation lines two spaces. mdformat skips this file.

```markdown
<!-- Bad: the code change, no context -->
- Route model access queries through `node_access_request`.

<!-- Good: what the user sees, and when -->
- Model dropdowns no longer mark every model "Not permitted by your license" when two installed
  libraries provide a node with the same name.
  [#5618](https://github.com/griptape-ai/griptape-nodes-engine/issues/5618)

<!-- Bad: vague -->
- Fix group node ports.

<!-- Good -->
- Group nodes in a reopened workflow now show the ports and connections of parameters added to
  the group.
  [#5563](https://github.com/griptape-ai/griptape-nodes-engine/issues/5563)
```

**Mark breaking changes** - A change that makes a saved workflow, node library, or request API client stop working without edits starts with `**Breaking:**`, stays under its type (usually `Changed` or `Removed`), and comes first in that list. Say what breaks and what to do. Long upgrade steps go in `MIGRATION.md`; link the section.

```markdown
- **Breaking:** `AgentStreamEvent` and the other agent streaming events now require a `thread_id`.
  See [MIGRATION.md](MIGRATION.md#agent-streaming-payloads-carry-thread_id).
```

**Leave released sections alone** - Do not add, rename, or reorder version headings or the link definitions at the bottom. `make version/publish` rolls `[Unreleased]` into the released version. Fixing a mistake in a released entry is fine.

## Architecture

**Engine owns the managers** - `Engine` (`retained_mode/engine.py`) holds ~28 managers (e.g. `FlowManager`, `NodeManager`) and injects itself into each one. Managers extend `EngineScoped` and reach peers via `self.engine.flow_manager`, not through process-wide state. `Engine` is a plain class, so tests and embedders can construct as many as they need.

**One global, at the edge** - `GriptapeNodes` (`retained_mode/griptape_nodes.py`) is a thin facade whose classmethods delegate to `current_engine()`. It is the only intentionally global entry point, and it exists for callers that cannot be handed a reference: saved workflow `.py` files (generated code carrying a `schema_version`), separately-versioned node libraries, and process entry points like the CLI. Engine-internal code must not use it; a `TID251` ban in `pyproject.toml` enforces this, with an allowlist that separates legitimate facade users from code not yet migrated.

**Event-driven operations** - All operations flow through request/response event dataclasses defined in `retained_mode/events/`, routed by `GriptapeNodes.handle_request()`.

**Library registration flow** - Libraries are defined by `griptape_nodes_library.json` files and registered via the `LibraryRegistry`. Node creation flows through `LibraryRegistry.create_node()` -> `Library.create_node()`. `LibraryRegistry` is deliberately process-global: it hands out node classes imported into `sys.modules`, so per-engine copies would advertise isolation the module system does not provide. `WorkflowRegistry` is still process-global too, but less defensibly: which workflows exist depends on the engine's workspace config, so it should become engine-owned.

**Custom nodes** - Extend `BaseNode` from `exe_types/node_types.py`. Parameters, traits, groups, and messages live in `exe_types/elements/`, one module per concern; import them from `exe_types/core_types.py`, which is the surface node libraries bind to. Flows and connections are defined in `exe_types/flow.py` and `exe_types/connections.py`.
