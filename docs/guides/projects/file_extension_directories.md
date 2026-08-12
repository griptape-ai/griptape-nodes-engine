# File Extension Directories

`file_extension_directories` is a project-template mapping from file extension to a folder fragment used to route files by type. When a situation macro references the derived variable `{file_extension_directory}`, the project system looks up the file's extension in this table and substitutes the associated value.

Typical use: keep images, videos, audio, and documents in separate subfolders under `outputs/` without writing a separate situation for each type.

## Quick example

```yaml
project_template_schema_version: "0.3.0"
name: "My Project"

file_extension_directories:
  png: "images"
  jpg: "images"
  mp4: "videos"
  wav: "audio"

situations:
  save_node_output:
    macro: "{outputs}/{file_extension_directory?:/}{node_name?:_}{file_name_base}{_index?:03}.{file_extension}"
```

With this configuration:

```
file_extension="png" → outputs/images/Node_render.png
file_extension="mp4" → outputs/videos/Node_render.mp4
file_extension="xyz" → outputs/Node_render.xyz        (unmapped: slot collapses)
```

The `?:/` on `{file_extension_directory?:/}` makes the slot optional and adds a trailing `/` when the value is present — so unmapped extensions land at the situation's directory root instead of failing.

## Two value forms

A value can be either a **plain name** or a **macro**.

### Plain name

```yaml
file_extension_directories:
  png: "images"
```

The string is used verbatim. No resolution happens. This is the common case and has zero overhead.

### Macro value

```yaml
file_extension_directories:
  mp4: "{outputs}/videos"
  wav: "{workspace_dir}/shared/audio"
```

A value containing `{...}` is resolved against the project's builtins, directory definitions, and any caller-supplied context (like `node_name`) before being substituted into the situation macro.

Macro values are what let a single `file_extension_directories` table reroute certain types to entirely different roots — e.g. video to a share drive — without writing a separate situation per type.

### What a macro value can reference

| Source                  | Example                                                                                      | Available?                               |
| ----------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Builtin variables       | `{workspace_dir}`, `{workflow_dir}`, `{project_dir}`, `{project_name}`, `{static_files_dir}` | Yes                                      |
| Directory definitions   | `{outputs}`, `{inputs}`, `{temp}`, any custom directory                                      | Yes                                      |
| Caller-supplied context | `{node_name}`, `{parameter_name}`, `{sub_dirs}`, `{_index}`                                  | Yes                                      |
| Filename parts          | `{file_name_base}`, `{file_extension}`                                                       | **No** — routing is not a filename layer |

Filename parts are excluded intentionally: `file_extension_directories` is a *routing* layer that decides which folder a file lands in. Filenames belong to the situation macro's filename section.

## How resolution works

`file_extension_directory` is a **derived variable**. It is not a builtin, and callers don't supply it directly. Instead, the project system runs a small derivation rule whenever the situation's macro template references it:

1. The caller names a situation and provides variables (including `file_extension`).
1. Before the situation macro is resolved, the derivation rule fires:
    - If `file_extension_directory` is already set by the caller, the rule abstains (caller wins).
    - Otherwise, the rule looks up `file_extension` (case-insensitive) in the current project's `file_extension_directories` table.
    - If the value is plain, it becomes the variable's value directly.
    - If the value is a macro, it is resolved to a concrete path string first.
1. The resulting value is injected into the variable bag and the situation macro is resolved as usual.

If lookup fails (empty extension, no project loaded, unmapped extension, or resolution error) the variable is simply not set. Situation macros that use the optional form `{file_extension_directory?:/}` degrade cleanly to no folder prefix; macros using the required form `{file_extension_directory}` fail resolution, as they would for any missing required variable.

## Interaction with the situation macro

There is no special-case engine logic that prepends or reparents the routing prefix. What you get is exactly what the situation macro template says you get.

| Situation macro shape                                               | Routing behavior                                                                                  |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `{outputs}/{file_extension_directory?:/}{file_name_base}.{ext}`     | Routing is a **subfolder** under `{outputs}`. Values must be relative.                            |
| `{file_extension_directory?:/}{file_name_base}.{ext}`               | Routing dictates the **root**. Values may be absolute to redirect away from `{outputs}` entirely. |
| `{outputs}/{file_extension_directory?:/}...` with an absolute value | String concatenation — `outputs//Volumes/share/videos/foo.mp4` — not what you want.               |

Choose the situation-macro shape that matches the kind of routing you need.

## Overlay merge behavior

`file_extension_directories` merges entry-by-entry, identically to `environment`:

- Keys not present in the overlay are inherited from the base.
- Keys present in the overlay override the base entry for that extension.
- Keys with a `null` value in the overlay are tombstoned — the base entry is dropped.

```yaml
# Inherit the base's image routing, send mp4 somewhere else, drop csv routing.
file_extension_directories:
  mp4: "{workspace_dir}/shared/videos"
  csv: null
```

## Caller override

Any caller can pre-populate `file_extension_directory` in the variables bag. When it's already set, the derivation rule abstains and the caller's value is used as-is. This is how UI-level settings (like an explicit output-folder override) bypass the taxonomy while still participating in the same situation macro.

## What's in the defaults

The system defaults ship with entries for common image, video, audio, text, and Python source extensions, routing each to `images`, `videos`, `audio`, `text`, and `python` subfolders respectively. You can override individual entries, add new ones, or tombstone any you don't want.
