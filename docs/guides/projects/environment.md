# Environment & Builtin Variables

## Environment

The `environment` section of a project file holds custom key-value pairs. These values are available for use in macros and directory `path_macro` fields.

```yaml
environment:
  RENDER_STYLE: "realistic"
  CLIENT_CODE: "ACME"
```

### Overlay behavior

Environment entries from your project file are merged on top of the system defaults. If a key exists in the defaults, your value replaces it. New keys are added.

### Referencing other variables from an environment value

Environment values are macros themselves. A value can reference a builtin, a directory, another project environment variable, or a shell environment variable by using `{NAME}`:

Assume the shell that launched Griptape Nodes has `SHARED_DRIVE=/mnt/renders` exported. Then:

```yaml
directories:
  outputs:
    # Reference a shell env var directly — no need to declare it under `environment:`
    path_macro: "{SHARED_DRIVE}/outputs"

environment:
  CLIENT_CODE: "ACME"
  # Reference a builtin
  PROJECT_RENDERS: "{project_dir}/renders"
  # Compose a shell env var, a project env var, and literal text
  CLIENT_RENDERS: "{SHARED_DRIVE}/{CLIENT_CODE}/renders"
```

References are resolved recursively, following the priority order in [Variable priority](#variable-priority). Cycles (for example, `A: "{B}"` and `B: "{A}"`) are detected and surfaced as macro resolution errors.

#### Legacy `$VAR` syntax

For backwards compatibility, an environment value that is **exactly** `$NAME` (no surrounding text, no suffix, no other macros) is expanded using the operating-system environment **when the value is consumed by a macro**:

```yaml
environment:
  OUTPUT_ROOT: "$RENDER_FARM_SHARE"  # works in macros: {OUTPUT_ROOT} -> /mnt/renders
```

Important limitations — the `$VAR` form is narrow and has known gaps:

- **Whole value only.** Anything appended (for example `"$SHARED_DRIVE/outputs"`) or any adjacent text is **not** expanded and is treated as a literal string. Macro resolution will fail looking up the entire trailing string as a secret name.
- **Macro-only, not process.** `$VAR` is only expanded at macro resolution time. It is **not** expanded when the value is written to `os.environ` — subprocesses and nodes calling `os.environ.get("OUTPUT_ROOT")` will see the literal string `"$RENDER_FARM_SHARE"`, not the expanded value.
- **Cannot be composed.** A `$`-prefixed value cannot reference another project env var, a builtin, or a directory.

Prefer the `{NAME}` form for all new projects — it composes cleanly, expands consistently in macros **and** `os.environ`, and works in both `environment` values and directory `path_macro` fields.

## Builtin variables

Builtin variables are automatically available in all macros. You do not define them — the system provides their values at runtime. They cannot be overridden.

| Variable           | Type      | Description                                                                                                                                                                  |
| ------------------ | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_dir`      | directory | Absolute path to the project base directory (the folder containing `griptape-nodes-project.yml`, or the workspace directory when no project file is present)                 |
| `workspace_dir`    | directory | Absolute path to the workspace directory (defaults to the project directory when no explicit workspace is configured; see [Workspace](workspace.md#config-resolution-order)) |
| `workflow_name`    | string    | Name of the currently running workflow                                                                                                                                       |
| `workflow_dir`     | directory | Absolute path to the directory containing the current workflow file                                                                                                          |
| `static_files_dir` | string    | Name of the static files subdirectory (from settings, defaults to `staticfiles`)                                                                                             |

### How builtins are resolved

Builtins are resolved at the moment a macro is evaluated — not when the project file is loaded. This means:

- `workflow_name` and `workflow_dir` reflect whichever workflow is currently executing
- `project_dir` reflects the actual path of the loaded project file
- `workspace_dir` reflects the project directory when no explicit workspace is configured, otherwise the value from the project-adjacent config or environment variable

If a builtin variable is required but cannot be resolved (for example, `workflow_name` when no workflow is running), macro resolution fails with an error. If the variable is optional (marked with `?`), the block is silently omitted instead.

### Builtin variables in situation macros

The `save_static_file` situation uses `workflow_dir` and `static_files_dir`:

```
{workflow_dir?:/}{static_files_dir}/{file_name_base}.{file_extension}
```

If `workflow_dir` is available, the static files go into a subdirectory of the workflow folder. If not (the workflow hasn't been saved yet), the `{workflow_dir?:/}` block is omitted.

## Variable priority

When a macro is resolved, variables are supplied from these sources in priority order:

1. **Builtin variables** — always win; cannot be overridden by any other source
1. **Directory names** — resolved from the project's directory definitions; cannot be overridden by caller-supplied variables
1. **Caller-supplied variables** — values passed by the node or operation requesting path resolution
1. **Derived variables** — computed from the variables above plus project state, and injected before the situation macro is resolved. A derived variable abstains when the caller has already supplied its value, so caller-supplied entries still win.
1. **Project environment variables** — values from the project's `environment:` block. Recursively resolved, so a project env value can reference builtins, directory names, other project env vars, or shell env vars.
1. **Shell environment variables** — final fallback. Any variable set in the shell that launched Griptape Nodes (including `HOME`, `USER`, or anything the user exported) can be referenced with `{NAME}` in a macro. A project env var of the same name always wins. Reserved names (builtins, directories) silently win over the shell, since shells have many incidental variables that shouldn't shadow project state.

If a caller tries to supply a value for a builtin or directory name that differs from the system value, the resolution fails with a `RESERVED_NAME_COLLISION` error.

### Derived variables

| Variable                   | Derived from                                                             | Source of truth                                                 |
| -------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------- |
| `file_extension_directory` | `file_extension` plus the project's `file_extension_directories` mapping | See [File Extension Directories](file_extension_directories.md) |
