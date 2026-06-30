# Workspace

The workspace is the root directory for all your work. It is the starting point from which relative file paths are resolved.

## Config files

Engine configuration is stored in files named `griptape_nodes_config.json`. Three locations are meaningful:

| File                                                  | Purpose                                                                          |
| ----------------------------------------------------- | -------------------------------------------------------------------------------- |
| `~/.config/griptape_nodes/griptape_nodes_config.json` | User config — global settings for this machine                                   |
| `<project_dir>/griptape_nodes_config.json`            | Project-adjacent config — shared defaults distributed alongside the project file |
| `<workspace_dir>/griptape_nodes_config.json`          | Workspace config — per-user overrides for the active project                     |

## Config resolution order

Settings are resolved in this order (later entries win):

1. Built-in defaults
1. User config (`~/.config/griptape_nodes/griptape_nodes_config.json`)
1. Project-adjacent config (`<project_dir>/griptape_nodes_config.json`)
1. Workspace config (`<workspace_dir>/griptape_nodes_config.json`)
1. Environment variable (`GTN_CONFIG_*`)

When the workspace directory is the same as the project directory (self-contained project), layers 3 and 4 point to the same file — it is loaded only once.

## Workspace resolution

When a project is loaded, its workspace directory is decided by the following sources, **highest priority first** — the first source that supplies a value wins:

1. The project's own `workspace_dir` field (see [Project-declared workspace](#project-declared-workspace-workspace_dir) below, and the [`workspace_dir` field](projects.md#workspace-directory) reference)
1. `project_workspaces` entry in user config (see [Per-project workspace overrides](#per-project-workspace-overrides) below)
1. Environment variable `GTN_CONFIG_WORKSPACE_DIRECTORY`
1. Project-adjacent config `workspace_directory` key
1. The nearest ancestor's resolved workspace, walking the explicit [parent-project chain](projects.md#parent-projects)
1. The global `workspace_directory` from user config, else the directory containing the project file (auto-default)

When no project file is involved, the workspace comes from the global `workspace_directory` (or the built-in `GriptapeNodes/` default beneath the engine's working directory).

## Per-project workspace overrides

The `project_workspaces` setting in your user config maps project file paths to workspace directory overrides. Use this when a shared project needs to resolve to a different local workspace on each machine.

```json
{
  "project_workspaces": {
    "//NAS/Projects/ProjectA/griptape-nodes-project.yml": "/Users/collin/ProjectA/",
    "//NAS/Projects/ProjectB/griptape-nodes-project.yml": "/Users/collin/ProjectB/"
  }
}
```

Keys are resolved absolute paths to project files. When a project is loaded, if its resolved path matches a key, the corresponding value is used as the workspace directory.

## Project-declared workspace (`workspace_dir`)

A project can name its own workspace directory in the project file via the `workspace_dir` field. This is the **highest-priority** workspace source: it beats the per-user `project_workspaces` mapping, the `GTN_CONFIG_WORKSPACE_DIRECTORY` env var, the project-adjacent config, parent inheritance, and the global default.

Use it when a project should always resolve to a fixed workspace, regardless of each user's machine-level config. Unlike `project_workspaces` (which every user must set in their own config), `workspace_dir` travels inside the project file, so the project carries its workspace with it.

When you create a **v1** project, the creation UI writes `workspace_dir: "./"` into it, so by default a v1 project is self-contained — its workspace is its own folder. Clearing the field (or omitting it) returns the project to inheriting the workspace from a parent or the global default. See [Schema versions](projects.md#schema-versions).

The value may be either a single path or a per-platform mapping:

```yaml
# A single path: absolute, or relative to this project file's directory.
workspace_dir: "./workspace"

# Or a per-platform mapping; the active platform's key is used, falling back to `default`.
workspace_dir:
  darwin: "/Volumes/fast/ProjectA"
  windows: "D:/ProjectA"
  default: "./workspace"
```

A relative path resolves against the directory containing the project file (the same way `parent_project_path` resolves), so a project that uses `workspace_dir: "./workspace"` stays portable: move or copy the project folder and the workspace follows. The target directory does not need to contain a `griptape_nodes_config.json` — an empty directory is valid.

See the [`workspace_dir` field reference](projects.md#workspace-directory) for the full schema, per-platform fallback rules, and how it behaves with parent projects.

## Example scenarios

### Scenario 1: Solo developer, no project file

No project file, no project-adjacent or workspace config. Workspace comes from user config or the built-in default.

```
~/.config/griptape_nodes/
  griptape_nodes_config.json    <- workspace_directory: ~/GriptapeNodes

~/GriptapeNodes/                <- workspace
  griptape_nodes_config.json    <- optional workspace-level overrides
```

### Scenario 2: Self-contained portable project

Project and config live in the same directory. The workspace auto-defaults to the project directory. Move the folder to a thumb drive or a different machine — no changes needed.

```
/My_Indie_Short/
  griptape-nodes-project.yml
  griptape_nodes_config.json    <- project-adjacent and workspace config (same file)
  inputs/
  outputs/
```

### Scenario 3: Shared project, per-user workspaces

A project file lives on a shared network drive. Each user maps it to their own local workspace via `project_workspaces` in their user config. Each user's workspace can have its own `griptape_nodes_config.json` for personal overrides.

```
//NAS/Projects/ProjectA/
  griptape-nodes-project.yml
  griptape_nodes_config.json    <- shared studio defaults (e.g. model preferences)
```

Collin's user config:

```json
{
  "project_workspaces": {
    "//NAS/Projects/ProjectA/griptape-nodes-project.yml": "/Users/collin/ProjectA/"
  }
}
```

James' user config:

```json
{
  "project_workspaces": {
    "//NAS/Projects/ProjectA/griptape-nodes-project.yml": "C:\\Projects\\ProjectA\\"
  }
}
```

Each user can place a `griptape_nodes_config.json` in their local workspace directory for personal overrides that take precedence over the shared project-adjacent config.

### Scenario 4: Studio-mandated workspace

The project-adjacent config sets a shared workspace. Artists without a `project_workspaces` entry all get the studio default. A workspace config on the shared drive can hold additional shared settings.

```
//NAS/Projects/ProjectA/
  griptape-nodes-project.yml
  griptape_nodes_config.json    <- workspace_directory: //NAS/Workspaces/ProjectA/

//NAS/Workspaces/ProjectA/
  griptape_nodes_config.json    <- shared workspace-level settings
```

Render farm machines can override the workspace via `GTN_CONFIG_WORKSPACE_DIRECTORY` without touching any shared files.

### Scenario 5: User overrides shared engine config

The studio ships `log_level: "WARNING"` in the project-adjacent config. A developer wants `DEBUG` locally. They put the override in their workspace config — workspace config (layer 4) beats project-adjacent (layer 3).

```
/Users/dev/ProjectA/
  griptape_nodes_config.json    <- {"log_level": "DEBUG"}
```

### Scenario 6: Multiple projects, different workspaces

`project_workspaces` maps each shared project to a distinct local workspace. Switching active projects switches workspace and reloads the workspace config.

```json
{
  "project_workspaces": {
    "//NAS/ProjectA/griptape-nodes-project.yml": "/Users/dev/ProjectA/",
    "//NAS/ProjectB/griptape-nodes-project.yml": "/Users/dev/ProjectB/"
  }
}
```

### Scenario 7: Workspace auto-discovery

Griptape Nodes looks for `griptape-nodes-project.yml` in the workspace directory on startup. If found, the project is loaded automatically. This is the same as scenario 2 — workspace and project directory are the same, so the single `griptape_nodes_config.json` serves as both project-adjacent and workspace config.

### Scenario 8: Project pins its own workspace

A project that should always resolve to a fixed workspace declares it directly with `workspace_dir`, so no per-user `project_workspaces` entry is needed. A relative value keeps the project portable across machines.

```yaml
# griptape-nodes-project.yml
name: "ProjectA"
workspace_dir: "./workspace"
```

```
/ProjectA/
  griptape-nodes-project.yml    <- workspace_dir: ./workspace
  workspace/                    <- resolved workspace (created on demand)
```

Because `workspace_dir` is the highest-priority source, this wins over any `project_workspaces` mapping or env var. Move the `ProjectA/` folder anywhere and the workspace still resolves to its `workspace/` subdirectory.

## How paths resolve

All relative paths in the project system resolve against the **workspace directory**. If your workspace is `/Users/you/workspace/` and a situation macro resolves to `outputs/render_001.png`, the final absolute path is `/Users/you/workspace/outputs/render_001.png`.

The **project base directory** (the folder containing `griptape-nodes-project.yml`) is exposed as the `{project_dir}` builtin variable but is not used as the resolution base for relative paths. It is used as a fallback when the path manager maps an absolute path back to a macro form and the path falls inside the project folder but outside any named directory.

## Workspace and the project file

When Griptape Nodes starts, it looks for `griptape-nodes-project.yml` in your workspace directory. If found, the file is merged on top of the system defaults to produce the active project template. If not found, system defaults are used.

See [Projects](projects.md) for details on the project file and merge model.

## Summary

| Setting                                      | Description                                         |
| -------------------------------------------- | --------------------------------------------------- |
| `workspace_dir`                              | Project-declared workspace; highest-priority source |
| `workspace_directory`                        | The root directory for your work                    |
| `project_workspaces`                         | Per-project workspace overrides in user config      |
| `griptape-nodes-project.yml`                 | Optional project template file                      |
| `<project_dir>/griptape_nodes_config.json`   | Optional shared config distributed with the project |
| `<workspace_dir>/griptape_nodes_config.json` | Optional per-user workspace config                  |
| `GTN_CONFIG_WORKSPACE_DIRECTORY`             | Environment variable override (highest priority)    |
