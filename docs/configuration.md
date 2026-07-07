# Engine Configuration

When running Griptape Nodes engine on your own machine, you are provided with utilities to manage configuration settings. Understanding how the configuration settings are loaded is important as you build out and manage more complicated projects or share projects with your team members.

> During installation, `gtn init` was run automatically.

> Looking for a specific setting? The [Configuration Reference](configuration_reference.md) lists every setting with its type, default, environment variable, and description, grouped by category.

## Configuration Loading

Griptape Nodes employs a specific search order to load settings from environment variables and configuration files. Understanding this process is key to managing your setup.

1. **Environment Variables (`.env`)**
    Environment variables are used to securely store sensitive secrets like API keys. Griptape Nodes automatically loads env files, making these secrets available to the application.

    - The primary `.env` file is loaded from the system-wide user configuration directory: `xdg_config_home() / "griptape_nodes" / ".env"` (commonly `~/.config/griptape_nodes/.env`).
    - This file is intended for secrets like `GT_CLOUD_API_KEY`, `OPENAI_API_KEY`.

    > You shouldn't interact with these files directly. Griptape Nodes manages your environment variables through its Settings dialog.

1. **Configuration Files (`griptape_nodes_config.json`)**
    Configuration files hold information important for Griptape Nodes operation, such as where to locate Node Libraries, as well as user preferences to customize the Griptape Nodes experience.

    - If no configuration files are found, Griptape Nodes will run using built-in default values.
    - Configuration files are always JSON (`griptape_nodes_config.json`). Settings are loaded from up to three such files plus a runtime override and environment variables, and merged in priority order:
    - **Load Order (lower numbers are loaded first; higher numbers override):**
        1. **Built-in defaults** — values baked into the application.
        1. **User config** — `~/.config/griptape_nodes/griptape_nodes_config.json`. Global settings for this machine.
        1. **Project-adjacent config** — `<project_dir>/griptape_nodes_config.json`. Loaded when a project is set as active. Use this to distribute shared defaults alongside a project file.
        1. **Workspace config** — `<workspace_dir>/griptape_nodes_config.json`. Loaded after the workspace is resolved. Use this for per-user overrides that take precedence over the shared project config. When the workspace directory is the same as the project directory, this file is the same as the project-adjacent config and is not loaded twice.
        1. **Per-project workspace override** — When the active project's path matches a key in the `project_workspaces` mapping (in your user config), that workspace directory is applied. This sets only `workspace_directory`, overriding any value from the config files above. See [Workspace](projects/workspace.md#per-project-workspace-overrides) for details.
        1. **Environment variables** — `GTN_CONFIG_*` prefix (highest priority). See below.
    - **Override Priority:** Settings in files loaded later override settings from files loaded earlier.

1. **Defaults and Merging**
    Griptape Nodes comes with built-in default settings for various options, including the default workspace directory. These defaults are used unless overridden by settings loaded from discovered configuration files.

    - Settings loaded from the first found configuration file override the built-in default values.
    - If no configuration file is found in any of the search paths, the application uses only the built-in defaults.
    - One key default is `workspace_directory`, which defaults to `<current_working_directory>/GriptapeNodes` if not specified in a loaded configuration file.

1. **Runtime Management (`ConfigManager`)**
    After initial settings are loaded, the `ConfigManager` handles runtime operations using the final resolved configuration, particularly the workspace directory. It's responsible for saving user-specific changes, like registered workflows, back to a configuration file within the workspace.

    - Once settings are loaded, the `ConfigManager` uses the final resolved `workspace_directory`.
    - Modifications made at runtime (e.g., registering custom workflows) are typically saved by the `ConfigManager` into a `griptape_nodes_config.json` file located within this resolved `workspace_directory`.

## Loading Examples

Here are a few scenarios to illustrate how configuration files are located and loaded:

**Scenario 1: Using Defaults**

- You run `gtn init` and accept the default settings.

- `gtn init` creates `~/.config/griptape_nodes/griptape_nodes_config.json` and `~/.config/griptape_nodes/.env`. It sets `workspace_directory` inside the `.json` file to point to `<current_directory_where_init_was_run>/GriptapeNodes`.

- You later run `gtn` from `/home/user/my_project/`.

- **File Structure:**

    ```
    /home/user/
        my_project/          <-- CWD when running 'gtn'
            GriptapeNodes/   <-- Default Workspace (may contain runtime saved config)
            my_flow.graph.json
        .config/
            griptape_nodes/
                .env                     # Loaded for environment variables
                griptape_nodes_config.json # Contains workspace_directory = /home/user/my_project/GriptapeNodes
    ```

- **Loading Process:**

    1. Loads built-in defaults.
    1. Loads `~/.config/griptape_nodes/griptape_nodes_config.json` (Found!), merging it over the defaults.
    1. **Result:** The `workspace_directory` is set to `/home/user/my_project/GriptapeNodes`. Subsequent runtime changes managed by `ConfigManager` will be saved to `/home/user/my_project/GriptapeNodes/griptape_nodes_config.json`.

**Scenario 2: Custom Workspace**

- You run `gtn init --workspace-directory /data/gtn_work`.

- `gtn init` creates `~/.config/griptape_nodes/griptape_nodes_config.json` (setting `workspace_directory = "/data/gtn_work"`) and `~/.config/griptape_nodes/.env`.

- You might manually create `/data/gtn_work/griptape_nodes_config.json` to store workspace-specific settings.

- You run `gtn` from `/home/user/some_dir/`.

- **File Structure:**

    ```
    /home/user/
        some_dir/            <-- CWD when running 'gtn'
        .config/
            griptape_nodes/
                .env                     # Loaded for environment variables
                griptape_nodes_config.json # Contains workspace_directory = /data/gtn_work
    /data/
        gtn_work/            <-- Custom Workspace
            griptape_nodes_config.json # Workspace config (also where runtime changes are saved)
            project_flows/
    ```

- **Loading Process:**

    1. Loads built-in defaults.
    1. Loads `~/.config/griptape_nodes/griptape_nodes_config.json` (Found!), merging it over the defaults. This sets `workspace_directory` to `/data/gtn_work`.
    1. Resolves the workspace and loads `/data/gtn_work/griptape_nodes_config.json` as the workspace config, merging it over the user config.
    1. **Result:** The `workspace_directory` is `/data/gtn_work`. Settings in the workspace config override the user config for this workspace. Runtime changes are saved back to `/data/gtn_work/griptape_nodes_config.json`.

**Scenario 3: No User Config (Built-in Defaults)**

- You haven't run `gtn init`, or you deleted `~/.config/griptape_nodes/`.

- You run `gtn` from `/home/user/my_project/` with no project active.

- **File Structure:**

    ```
    /home/user/
        my_project/          <-- CWD when running 'gtn'
            GriptapeNodes/   <-- Default workspace location
            my_flow.graph.json
    ```

- **Loading Process:**

    1. Loads built-in defaults.
    1. Checks `~/.config/griptape_nodes/griptape_nodes_config.json` (Assume not found).
    1. No project is active, so no project-adjacent config is loaded.
    1. **Result:** The application runs on built-in defaults. `workspace_directory` falls back to its default of `<current_working_directory>/GriptapeNodes` = `/home/user/my_project/GriptapeNodes`, and runtime changes are saved to a `griptape_nodes_config.json` created inside that workspace.

## Environment Variable Overrides

A top-level configuration value can be set or overridden using an environment variable with the `GTN_CONFIG_` prefix. The key is the config setting name in uppercase:

```
GTN_CONFIG_<SETTING_NAME>=<value>
```

Environment variable overrides have the **highest priority** — they win over user config files, project-adjacent config files, the per-project workspace override, and built-in defaults.

Examples:

| Setting               | Environment variable             |
| --------------------- | -------------------------------- |
| `workspace_directory` | `GTN_CONFIG_WORKSPACE_DIRECTORY` |
| `libraries_directory` | `GTN_CONFIG_LIBRARIES_DIRECTORY` |
| `project_file`        | `GTN_CONFIG_PROJECT_FILE`        |
| `log_level`           | `GTN_CONFIG_LOG_LEVEL`           |
| `storage_backend`     | `GTN_CONFIG_STORAGE_BACKEND`     |

This is useful for scripted environments, containers, and CI/CD pipelines where you want to inject configuration without modifying any config files:

```bash
GTN_CONFIG_PROJECT_FILE=/shared/studio-project.yml gtn
```

> **Only top-level settings can be set this way.** `GTN_CONFIG_*` variables map to a single top-level config key; they cannot reach nested settings (anything under `app_events`, `worker`, or `agent`, such as `libraries_to_register`). To change a nested setting, edit a `griptape_nodes_config.json` file. The [Configuration Reference](configuration_reference.md) marks which settings have an environment variable.

### Recursive Discovery Depth (`discovery_max_depth`)

When `projects_to_register`, `libraries_to_register`, or `workflows_to_register` points at a directory, the engine recursively scans it for the relevant files on startup (project files, library manifests, and workflow files respectively). The scan is depth-bounded so a pathologically deep tree (or a symlink loop) can't stall the boot sequence. The `discovery_max_depth` setting controls that cap; its default is **5** directory levels below the registered directory, which comfortably covers normal layouts.

Being a normal setting, it can be set in any config file or overridden with the `GTN_CONFIG_DISCOVERY_MAX_DEPTH` environment variable:

```bash
GTN_CONFIG_DISCOVERY_MAX_DEPTH=20 gtn   # scan deeper-nested layouts
```

A value of `0` scans only the top-level directory (no subdirectories).

## Workspace Directory

During `gtn init`, you specify a Workspace Directory. This is the root for your projects, saved flows, and potentially project-specific settings.

While `gtn init` might suggest `<current_working_directory>/GriptapeNodes` as a default, you can choose any location. Griptape Nodes uses the exact path you provide, which is then stored in the system `griptape_nodes_config.json`.

It does **not** automatically search within a hardcoded `GriptapeNodes` subdirectory; it relies solely on the configured path.

### Separating Libraries from the Workspace

By default, downloaded libraries live in a `libraries/` folder **inside** the workspace, because `libraries_directory` is a relative path resolved against `workspace_directory`. If your workspace is on a slow or remote drive (a network share, a mounted volume), keeping libraries there can degrade performance, since the engine reads library code from disk frequently.

You can point `libraries_directory` at an **absolute** path on fast local storage while keeping the workspace (projects, workflows, generated assets) on the remote drive. When `libraries_directory` is absolute, it is used as-is and the workspace location is ignored for libraries:

```json
{
    "workspace_directory": "/Volumes/team-share/GriptapeNodes",
    "libraries_directory": "/Users/me/.griptape-nodes-libraries"
}
```

The same equivalently set via an environment variable:

```bash
GTN_CONFIG_LIBRARIES_DIRECTORY=/Users/me/.griptape-nodes-libraries gtn
```

The same relative-vs-absolute rule applies to `sandbox_library_directory` and `static_files_directory`, so you can independently relocate any of them onto local storage while the workspace stays remote.

`libraries_directory` here is the machine-wide default. An individual project can override it with its own [`libraries_dir`](projects/projects.md#libraries-directory) field in the project file, which takes precedence over this config value and travels with the project (and is inherited by child projects). Use the config setting to relocate libraries for the whole engine; use the project field when a specific project or project tree needs its own shared library location.

## Static File Server Configuration

When running Griptape Nodes, a local static file server hosts media assets (images, videos, audio) generated by your workflows. The `static_server_base_url` setting controls what base URL is used when generating links to these files. By default, it uses `http://localhost:8124`, but you can override this when using tunnels, proxies, or deploying in containers.

### When to Override This Setting

You'll need to configure `static_server_base_url` in these scenarios:

- **Tunneling Services**: Using ngrok, cloudflare tunnels, or similar services to expose your local server
- **Docker/Kubernetes**: Running in containers where the internal address differs from the external access point
- **Reverse Proxies**: Running behind nginx, Apache, or other reverse proxies
- **Remote Development**: Working on a remote machine and accessing the UI from your local browser
- **Team Collaboration**: Sharing your running instance with team members who need to access generated media

### How to Configure

The static server base URL is configured using the `static_server_base_url` setting in the Griptape Nodes UI Settings dialog. If not explicitly set, it defaults to `http://localhost:8124` (or respects `STATIC_SERVER_HOST` and `STATIC_SERVER_PORT` environment variables if they are set).

After updating this setting, you'll need to restart the Griptape Nodes engine for the changes to take effect.

### Example Scenarios

**Scenario 1: Local Development with ngrok**

You're testing webhook integrations that need to access generated media files.

1. Start ngrok tunnel: `ngrok http 8124`
1. Copy the generated URL (e.g., `https://abc123.ngrok.app`)
1. Open Griptape Nodes UI Settings dialog
1. Update the `static_server_base_url` setting with your ngrok URL
1. Restart the Griptape Nodes engine: `gtn`

Now when workflows generate media:

- Local access: Works via the tunnel URL
- External services: Can fetch media via the ngrok URL
- CORS: Automatically configured for the tunnel URL
