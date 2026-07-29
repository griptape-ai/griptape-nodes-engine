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

## Documentation

**Update docs with user-facing changes** - When a change affects what users see or do, update the documentation in the same PR. Common mappings:

- New or changed CLI commands/flags → `docs/reference/command_line_interface.md`
- New or changed settings → `docs/reference/configuration_reference.md` (and `docs/guides/configuration.md` if it needs explanation)
- New user-facing features or request event families → the relevant page under `docs/guides/`, or a new page
- Editor-facing behavior changes (shortcuts, menus, panels) → `docs/guides/editor/`
- Deprecated or renamed nodes → `MIGRATION.md` and the node's doc page

**Wire new pages into mkdocs.yml twice** - A new docs page must be added to both the `nav` section and the `llmstxt` plugin sections in `mkdocs.yml`. Verify with `uv run mkdocs build --strict`.

**Write for artists** - Docs follow the same rule as error messages: understandable by artists, not just engineers. Use exact UI labels, menu paths, and shortcuts. Match the voice of existing pages such as `docs/guides/libraries.md`.

## Architecture

**Singleton managers** - `GriptapeNodes` is a singleton holding 25+ managers (e.g., `FlowManager`, `NodeManager`), each accessed via `GriptapeNodes.ManagerName()` classmethods.

**Event-driven operations** - All operations flow through request/response event dataclasses defined in `retained_mode/events/`, routed by `GriptapeNodes.handle_request()`.

**Library registration flow** - Libraries are defined by `griptape_nodes_library.json` files and registered via the `LibraryRegistry` singleton. Node creation flows through `LibraryRegistry.create_node()` -> `Library.create_node()`.

**Custom nodes** - Extend `BaseNode` from `exe_types/node_types.py`. Parameters, flows, and connections are defined in `exe_types/core_types.py` and `exe_types/node_types.py`.
