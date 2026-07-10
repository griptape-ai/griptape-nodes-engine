# Situations

A situation is a named file-saving scenario. It defines:

- **Where** the file goes (via a macro template)
- **How** to handle the case when a file already exists there (via a collision policy)
- **What to do** if saving fails (via an optional fallback situation)

When a node needs to save a file, it names the situation it's in (for example, `save_node_output`) and the project system resolves the path using that situation's macro and applies the policy.

## Collision policies

| Policy       | Behavior                                                                                                                                                                                                                                                                                                                            |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create_new` | Increment a counter in the filename until a non-colliding name is found. The macro can include `{_index?:NN}` (optional — absent on the first save, indexed on collision) or `{_index:NN}` (required — indexed from the first save). If neither is present, the system appends `_1`, `_2`, … to the resolved filename on collision. |
| `overwrite`  | Replace the existing file without asking.                                                                                                                                                                                                                                                                                           |
| `fail`       | Stop and report an error if the file already exists.                                                                                                                                                                                                                                                                                |

The `create_dirs` field controls whether intermediate parent directories are created automatically (`true`, like `mkdir -p`) or whether a missing parent directory causes an error (`false`).

## Fallbacks

A situation can name a fallback situation. If the primary situation cannot resolve its macro (for example, because required variables are missing), the system tries the fallback. The default `save_file` situation is a minimal fallback used by most other situations.

## Default situations

### `save_file`

```
macro:  {file_name_base}{_index?:03}.{file_extension}
policy: create_new, create_dirs: true
```

Generic file save at the project root (or wherever the caller's path context puts it). This is the fallback for most other situations. The `{_index?:03}` variable is zero-padded and optional — omitted on the first save, then `001`, `002`, … on collision (padded width preserved across the sequence).

### `copy_external_file`

```
macro:    {inputs}/{node_name?:_}{parameter_name?:_}{file_name_base}{_index?:03}.{file_extension}
policy:   create_new, create_dirs: true
fallback: save_file
```

Used when a user copies or drags an external file into the project. The file is placed in the `inputs` directory. The node name and parameter name are prepended as optional prefixes to help identify the file's origin.

**Example:**

```
node_name="LoadImage", parameter_name="source", file_name_base="photo", file_extension="jpg"
→ inputs/LoadImage_source_photo.jpg

node_name not provided, file_name_base="photo", file_extension="jpg"
→ inputs/photo.jpg
```

### `download_url`

```
macro:    {inputs}/{sanitized_url}
policy:   overwrite, create_dirs: true
fallback: save_file
```

Used when a node downloads a file from a URL. The URL is sanitized into a safe filename. Files downloaded from the same URL are overwritten rather than duplicated.

### `save_node_output`

```
macro:    {outputs}/{sub_dirs?:/}{node_name?:_}{file_name_base}{_index?:03}.{file_extension}
policy:   create_new, create_dirs: true
fallback: save_file
```

Used when a node generates and saves output. Files go into the `outputs` directory. Optional sub-directories (`{sub_dirs?:/}`) allow nesting within outputs. The node name is an optional prefix.

**Example:**

```
outputs="outputs", node_name="ImageGen", file_name_base="render", _index=1, file_extension="png"
→ outputs/ImageGen_render001.png

sub_dirs="lighting/pass_a", node_name="ImageGen", file_name_base="render", file_extension="exr"
→ outputs/lighting/pass_a/ImageGen_render.exr
```

### `save_preview`

```
macro:    {previews}/{drive_volume_mount?:/}{source_relative_path?:/}{source_file_name}.{preview_format}
policy:   overwrite, create_dirs: true
fallback: save_file
```

Used to generate preview thumbnails. Previews mirror the directory hierarchy of the source file so that each source file has exactly one preview. Previews are overwritten rather than versioned. The `previews` directory defaults to `.griptape-nodes-previews` (a hidden folder).

### `save_static_file`

```
macro:    {workflow_dir?:/}{static_files_dir}/{file_name_base}.{file_extension}
policy:   overwrite, create_dirs: true
fallback: save_file
```

Used by the static files manager to save static assets. Files go into the `static_files_dir` subdirectory of the current workflow's directory. These files are overwritten when regenerated.

### `save_temp_file`

```
macro:    {temp}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}
policy:   overwrite, create_dirs: true
fallback: save_file
```

Used when a node needs to write an intermediate or scratch file during processing (for example, a temporary EXR written between color-space conversion steps). Files go into the `temp` directory and should be deleted by the node after use.

### `save_workflow`

```
macro:    {workspace_dir}/{sub_dirs?:/}{file_name_base}.{file_extension}
policy:   overwrite, create_dirs: true
fallback: save_file
```

Used when a workflow is saved. The workflow file goes into the workspace root, preserving any sub-directory hierarchy via the optional `{sub_dirs?:/}` prefix. Saving a workflow overwrites the existing file rather than versioning it; to produce a numbered sequence of saves instead, see [`create_versioned_workflow`](#create_versioned_workflow) below.

**Example:**

```
workspace_dir="/projects/demo", file_name_base="my_workflow", file_extension="py"
→ /projects/demo/my_workflow.py

sub_dirs="archived", file_name_base="my_workflow", file_extension="py"
→ /projects/demo/archived/my_workflow.py
```

### `create_versioned_workflow`

```
macro:    {workspace_dir}/{sub_dirs?:/}{file_name_base}_v{_index:03}.{file_extension}
policy:   create_new, create_dirs: true
fallback: save_file
```

Used when a workflow is saved with the versioned-save intent. Every save produces a new file with the next padded index in the sequence — `my_workflow_v001.py`, `my_workflow_v002.py`, … — so users can keep snapshots without overwriting earlier work.

The version-bump is **macro-driven**: when a versioned save runs, the engine reverse-matches the previous save's path against this situation's macro and extracts every variable the macro defines, including the bound padded slot. The next save reuses those variables verbatim, and the collision walk advances the padded index past any existing files. Because nothing about the version suffix is hardcoded, customizing the macro (e.g. swapping `_v{_index:03}` for `.{_index:04}`) still works — the new pattern becomes the contract for both forward saves and reverse-matches.

> **Tip:** Custom projects can switch the auto-index slot to the more explicit `_v{###}` syntax (see [Sequence slot (`{###}`)](macros.md#sequence-slot-)). It behaves identically to `{_index:03}` for the default 3-digit case and overflows naturally past `999` instead of staying zero-padded.

This situation is selected at the API layer by passing `create_versioned=True` on `SaveWorkflowRequest`; the UI exposes it as a separate menu item (e.g. "Save New Version"). See [Macros — Numeric padding](macros.md#numeric-padding) for the auto-index contract.

> **Note**: Customizing `save_workflow` to use `create_new` directly (instead of using `create_versioned_workflow` + the flag) emits a warning at save time. The configuration still works — the first save lands at `_v001` — but every subsequent save hits the in-place overwrite branch and writes back to `_v001` rather than advancing to `_v002`. Use `create_versioned_workflow` for true versioning.

**Example:**

```
workspace_dir="/projects/demo", file_name_base="my_workflow", file_extension="py"
  First save  → /projects/demo/my_workflow_v001.py
  Second save → /projects/demo/my_workflow_v002.py
  Third save  → /projects/demo/my_workflow_v003.py
```

## How nodes use situations

Nodes that save files use a `ProjectFileParameter` to declare which situation they operate in. The node provides its situation-specific variables (like `file_name_base` and `file_extension`), and the project system supplies everything else (directory paths, builtin variables).

To use a custom situation from your project file, configure the node's situation parameter to match the name of your custom situation.

## Adding custom situations

See [Customization Guide](customization.md) for examples.
