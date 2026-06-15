# Projects

A project template is a configuration that defines how files are organized and saved. It is the combination of [situations](situations.md), [directories](directories.md), and [environment variables](environment.md) that every node consults when it needs to save a file.

By default, Griptape Nodes ships with a built-in project template that handles the common cases — saving images, audio, and other outputs to an `outputs` folder in your workspace. A project file lets you change any of that: redirect where a particular kind of file is saved, add a new named location (like `renders/4k`), or inject environment variables specific to your project. You only write down the things you want to change; everything else continues to use the [default situations](situations.md#default-situations) and [default directories](directories.md#default-directories).

## The project file

Your workspace-level customizations live in a file named:

```
griptape-nodes-project.yml
```

Place this file in your workspace directory. It is optional — if absent, the [system defaults](#the-system-defaults) apply.

## Project file structure

```yaml
project_template_schema_version: "0.3.2"
name: "My Project"
description: "Optional description"

directories:
  outputs:
    path_macro: "renders"       # override the outputs directory path
  renders_4k:                   # add a new directory
    path_macro: "renders/4k"

situations:
  save_node_output:             # override an existing situation
    macro: "{outputs}/{workflow_name?:_}{file_name_base}{_index?:03}.{file_extension}"
  archive_render:               # add a new situation
    macro: "{renders_4k}/{workflow_name?:_}{file_name_base}.{file_extension}"
    policy:
      on_collision: overwrite
      create_dirs: true

environment:
  PROJECT_CODENAME: "aurora"

file_extension_directories:
  png: "images"
  mp4: "{outputs}/videos"
```

### Fields reference

| Field                             | Required | Description                                                                                                                         |
| --------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `project_template_schema_version` | Yes      | Must match the supported version (`"0.3.2"`)                                                                                        |
| `name`                            | Yes      | Human-readable name for this project                                                                                                |
| `description`                     | No       | Optional description                                                                                                                |
| `parent_project_path`             | No       | Path to a parent project YAML; the parent's merged template becomes the base for this one. See [Parent projects](#parent-projects). |
| `situations`                      | No       | Dict of situation overrides and additions                                                                                           |
| `directories`                     | No       | Dict of directory overrides and additions                                                                                           |
| `environment`                     | No       | Dict of custom key-value variables                                                                                                  |
| `file_extension_directories`      | No       | Extension-to-folder routing; see [File Extension Directories](file_extension_directories.md)                                        |

### Situation fields

Each entry under `situations` is keyed by situation name. You can provide any subset of these fields:

| Field                 | Required for new | Description                                        |
| --------------------- | ---------------- | -------------------------------------------------- |
| `macro`               | Yes              | Macro template string for the file path            |
| `policy`              | Yes              | Must include both `on_collision` and `create_dirs` |
| `policy.on_collision` | Yes              | One of: `create_new`, `overwrite`, `fail`          |
| `policy.create_dirs`  | Yes              | `true` to create missing directories automatically |
| `fallback`            | No               | Name of another situation to use if this one fails |
| `description`         | No               | Human-readable description                         |

When *modifying* an existing situation, you only need to provide the fields you want to change. When *adding* a new situation, `macro` and `policy` are required.

### Directory fields

Each entry under `directories` is keyed by the logical name:

| Field        | Required for new | Description                                                                                                                                           |
| ------------ | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `path_macro` | Yes              | Path string (may contain macros or environment variable references), or a per-platform mapping. See [Directories](directories.md#per-platform-paths). |

## The merge model

The project system uses a layered model:

1. **System defaults** — a complete, built-in project template that ships with Griptape Nodes. It defines all default situations and directories and is always loaded first.

1. **Parent chain** (optional) — when a project declares `parent_project_path`, the parent's merged template is loaded recursively and replaces the system defaults as this project's base. See [Parent projects](#parent-projects).

1. **This project's overlay** — the contents of this `griptape-nodes-project.yml`. It is merged *on top of* whatever base resolved above.

The merge behavior is additive and field-level:

- Situations and directories from the overlay are merged into the base. An overlay situation with the same name as a base situation changes only the fields you specify (e.g., just the macro, or just the policy). An overlay situation with a new name is added alongside the base.
- Environment entries in the overlay override entries with the same key in the base. New keys are added.
- `file_extension_directories` entries merge per-key the same way as environment entries. A `null` value tombstones the base entry.
- The `name` field is always taken from the overlay (required).

You never need to repeat inherited values. Your project file only needs to contain the things you want to change.

## Parent projects

A project can declare another project as its parent. The parent's fully merged template (after the parent has resolved its *own* parent chain) becomes the base, and this project's overlay is applied on top. This lets you keep a shared base configuration in one place — a team-wide directory layout, a fixed set of environment variables, a custom situation — and let derived projects inherit from it instead of restating every field.

```yaml
project_template_schema_version: "0.3.2"
name: "Marketing Renders"
parent_project_path: "../team-base/griptape-nodes-project.yml"

# Only the diffs against the parent need to be listed.
directories:
  outputs:
    path_macro: "renders/marketing"
```

### Path forms

`parent_project_path` accepts two forms:

| Form     | Example                                                      | Notes                                                                                                                          |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Relative | `../team-base/griptape-nodes-project.yml`                    | Preferred. Resolved against the directory of *this* project's YAML file, so the link survives moves between machines and OSes. |
| Absolute | `/Users/alice/projects/team-base/griptape-nodes-project.yml` | Bakes in a per-machine path; works locally but does not travel.                                                                |

No macros are expanded inside `parent_project_path`. Tokens like `{workspace_dir}`, `{project_dir}`, and `{outputs}` are not substituted here — a path containing them is treated literally. Macros belong in `path_macro` and `situations.*.macro` fields.

### Inheritance and tombstones

The parent chain is resolved before merge. A grandchild → child → parent → defaults chain composes the same way the workspace overlay composes onto defaults: each level's fields override or extend the level beneath it.

To **drop** an inherited entry rather than override it, set the value to `null`:

```yaml
directories:
  scratch: null   # remove the parent's `scratch` directory entirely
```

Setting `parent_project_path: null` explicitly clears an inherited link, falling the project back to the system defaults as its base. Omitting the field entirely inherits the parent's link (which is rarely what you want — typically you set `parent_project_path` on each child explicitly).

A project whose base *is* the system defaults should leave `parent_project_path` out entirely — absence already means "system defaults are the base," so there is nothing to point at. Only set the field when the parent is a *different* project file. This matters for sharing: writing a parent path into a project saved to shared or synced storage bakes in a machine-specific link that will not resolve on another person's machine, so a default-derived project should carry no path at all.

### Cycles and missing parents

The engine refuses to load a project whose parent chain contains a cycle. A direct self-reference, A → B → A, and longer cycles are all caught and reported as a validation error on the child being loaded.

A parent that cannot be read or parsed surfaces as a validation error on the child as well. The child is not silently downgraded to the system defaults — the load fails so the problem is visible.

### Sharing across machines

A workspace zipped on one machine and unpacked on another will keep parent links intact as long as the parent is referenced with a relative path and travels alongside the child inside the workspace. Absolute paths break across machines (different home directories, different drive letters); the relative form is what makes parent chains portable.

## Validation status

When a project file is loaded, it receives one of four statuses:

| Status     | Meaning                                                                |
| ---------- | ---------------------------------------------------------------------- |
| `GOOD`     | Loaded and fully valid                                                 |
| `FLAWED`   | Loaded but has warnings (e.g., schema version mismatch) — still usable |
| `UNUSABLE` | Errors prevent the template from being used                            |
| `MISSING`  | The project file was not found                                         |

When a project file has `UNUSABLE` or `MISSING` status, Griptape Nodes falls back to the system defaults. You can inspect validation problems (with field paths and line numbers) to diagnose issues.

## The system defaults

The system defaults define these situations and directories out of the box. See [Situations](situations.md) and [Directories](directories.md) for full details.
