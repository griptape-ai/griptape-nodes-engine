<!-- GENERATED FILE - DO NOT EDIT BY HAND.
     Regenerate with `make docs/settings-reference` after changing the Settings model. -->

# Configuration Reference

Every Griptape Nodes engine setting, grouped by category. Each setting can be placed in any `griptape_nodes_config.json` file (see [Engine Configuration](configuration.md) for the load order). Settings with a `GTN_CONFIG_*` env var can also be overridden from the environment; nested settings must be edited in a config file.

## File System

Directories and file paths for the application

| Setting                          | Type    | Default                                     | Environment variable                        | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------- | ------- | ------------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `workspace_directory`            | string  | `<current_working_directory>/GriptapeNodes` | `GTN_CONFIG_WORKSPACE_DIRECTORY`            | Root directory for projects, workflows, and generated assets. Defaults to a GriptapeNodes folder under the current working directory. The other File System paths (libraries_directory, static_files_directory, sandbox_library_directory, synced_workflows_directory) are interpreted relative to this directory unless they are set to absolute paths.                                                                                                                               |
| `static_files_directory`         | string  | `"staticfiles"`                             | `GTN_CONFIG_STATIC_FILES_DIRECTORY`         | Path to the static files directory, relative to the workspace directory.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `sandbox_library_directory`      | string  | `"sandbox_library"`                         | `GTN_CONFIG_SANDBOX_LIBRARY_DIRECTORY`      | Path to the sandbox library directory (useful while developing nodes). Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is.                                                                                                                                                                                                                                                                                                              |
| `libraries_directory`            | string  | `"libraries"`                               | `GTN_CONFIG_LIBRARIES_DIRECTORY`            | Path to directory for downloaded libraries. All griptape_nodes_library.json files found recursively will be auto-discovered on startup. Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is. A project may override this location via the project-template `libraries_dir` field (inheritable down the parent-project chain), which takes precedence over this value so a child project can share its parent's library install location. |
| `synced_workflows_directory`     | string  | `"synced_workflows"`                        | `GTN_CONFIG_SYNCED_WORKFLOWS_DIRECTORY`     | Path to the synced workflows directory, relative to the workspace directory.                                                                                                                                                                                                                                                                                                                                                                                                           |
| `enable_workspace_file_watching` | boolean | `true`                                      | `GTN_CONFIG_ENABLE_WORKSPACE_FILE_WATCHING` | Enable file watching for synced workflows directory                                                                                                                                                                                                                                                                                                                                                                                                                                    |

## Application Events

Configuration for application lifecycle events

| Setting      | Type   | Default         | Environment variable           | Description                                                   |
| ------------ | ------ | --------------- | ------------------------------ | ------------------------------------------------------------- |
| `app_events` | object | (nested object) | n/a (nested; edit config file) | Nested settings; edit the sub-keys directly in a config file. |

## Execution

Workflow execution and processing settings

| Setting                   | Type                                                   | Default         | Environment variable                 | Description                                                                                                                                                                                                             |
| ------------------------- | ------------------------------------------------------ | --------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `log_level`               | one of `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG` | `"INFO"`        | `GTN_CONFIG_LOG_LEVEL`               | Logging verbosity for the engine. One of CRITICAL, ERROR, WARNING, INFO, or DEBUG, from least to most verbose.                                                                                                          |
| `workflow_execution_mode` | one of `sequential`, `parallel`                        | `"sequential"`  | `GTN_CONFIG_WORKFLOW_EXECUTION_MODE` | Workflow execution mode for node processing. SEQUENTIAL mode uses ParallelResolutionMachine with max_nodes_in_parallel=1 to execute nodes one at a time. PARALLEL mode uses the configured max_nodes_in_parallel value. |
| `max_nodes_in_parallel`   | integer                                                | `5`             | `GTN_CONFIG_MAX_NODES_IN_PARALLEL`   | Maximum number of nodes executing at a time for parallel execution.                                                                                                                                                     |
| `worker`                  | object                                                 | (nested object) | n/a (nested; edit config file)       | Nested settings; edit the sub-keys directly in a config file.                                                                                                                                                           |

## Storage

Data storage and persistence configuration

| Setting                         | Type                  | Default   | Environment variable                       | Description                                                                                                                                                      |
| ------------------------------- | --------------------- | --------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `storage_backend`               | one of `local`, `gtc` | `"local"` | `GTN_CONFIG_STORAGE_BACKEND`               | Backend used to persist workflow data and generated assets. 'local' stores files on the local filesystem under the workspace; 'gtc' uses Griptape Cloud storage. |
| `auto_inject_workflow_metadata` | boolean               | `true`    | `GTN_CONFIG_AUTO_INJECT_WORKFLOW_METADATA` | Automatically inject workflow metadata into saved files with supported formats                                                                                   |
| `thread_storage_backend`        | `"local"` (constant)  | `"local"` | `GTN_CONFIG_THREAD_STORAGE_BACKEND`        | Storage backend for conversation threads. Only 'local' (filesystem) is supported; Griptape Cloud support was removed in the Pydantic AI migration.               |

## System Requirements

System resource requirements and limits

| Setting                           | Type    | Default | Environment variable                         | Description                                                                                                                                                                                                                                                                                                       |
| --------------------------------- | ------- | ------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `minimum_disk_space_gb_libraries` | number  | `10.0`  | `GTN_CONFIG_MINIMUM_DISK_SPACE_GB_LIBRARIES` | Minimum disk space in GB required for library installation and virtual environment operations                                                                                                                                                                                                                     |
| `minimum_disk_space_gb_workflows` | number  | `1.0`   | `GTN_CONFIG_MINIMUM_DISK_SPACE_GB_WORKFLOWS` | Minimum disk space in GB required for saving workflows                                                                                                                                                                                                                                                            |
| `discovery_max_depth`             | integer | `5`     | `GTN_CONFIG_DISCOVERY_MAX_DEPTH`             | Maximum directory depth the engine walks when a registered entry points at a directory to recursively discover files (e.g. project files under projects_to_register). Bounds boot-time scans against pathologically deep trees and symlink loops. 0 scans only the top-level directory; each nested level adds 1. |

## MCP Servers

Model Context Protocol server configurations

| Setting       | Type  | Default | Environment variable           | Description                                          |
| ------------- | ----- | ------- | ------------------------------ | ---------------------------------------------------- |
| `mcp_servers` | array | `[]`    | n/a (nested; edit config file) | List of Model Context Protocol server configurations |

## Static Server

Static file server configuration for serving media assets

| Setting                  | Type   | Default | Environment variable                | Description                                                                                                                                                                                                                                                                                 |
| ------------------------ | ------ | ------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `static_server_base_url` | string | `null`  | `GTN_CONFIG_STATIC_SERVER_BASE_URL` | Base URL for the static server. Leave unset to derive it from the server's host/port (including the OS-assigned port when the configured port is unavailable). Set this only to override the derived URL, e.g. when fronting the server with a tunnel (ngrok, cloudflare) or reverse proxy. |

## Artifacts

Settings for artifact providers and preview generation

| Setting     | Type   | Default | Environment variable           | Description                                                         |
| ----------- | ------ | ------- | ------------------------------ | ------------------------------------------------------------------- |
| `artifacts` | object | `{}`    | n/a (nested; edit config file) | Control how previews are generated for images and other media files |

## Projects

Project template configurations and registrations

| Setting              | Type   | Default | Environment variable           | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------- | ------ | ------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_file`       | string | `null`  | `GTN_CONFIG_PROJECT_FILE`      | Path to the project file (griptape-nodes-project.yml) to load initially when the engine starts. When set, overrides the default location of \<workspace_directory>/griptape-nodes-project.yml. If the specified path does not exist, falls back to the workspace default. The sentinel value '<system-defaults>' means the engine deliberately stays on system defaults and suppresses the workspace-default fallback (so a workspace griptape-nodes-project.yml is not auto-discovered); this is what the engine persists when it is intentionally on system defaults. |
| `project_workspaces` | object | `{}`    | n/a (nested; edit config file) | Mapping of project identifiers to workspace directory overrides. A key may be either a project ID or a project file path: it is first matched against loaded project IDs, and if none match, treated as a project file path. When a project is loaded, if it matches a key here, the corresponding value is used as the workspace directory instead of the project-adjacent config or auto-default.                                                                                                                                                                     |

## Agent

Agent behavior and system prompt

| Setting | Type   | Default         | Environment variable           | Description                                                   |
| ------- | ------ | --------------- | ------------------------------ | ------------------------------------------------------------- |
| `agent` | object | (nested object) | n/a (nested; edit config file) | Nested settings; edit the sub-keys directly in a config file. |

## Libraries

Settings for library management and dependency installation

| Setting   | Type   | Default         | Environment variable           | Description                                                   |
| --------- | ------ | --------------- | ------------------------------ | ------------------------------------------------------------- |
| `library` | object | (nested object) | n/a (nested; edit config file) | Nested settings; edit the sub-keys directly in a config file. |
