# Griptape Nodes Command Line Interface (CLI)

If you're new to command-line interfaces (CLIs), a CLI is a text-based way to interact with software by typing commands instead of clicking buttons in a UI. Griptape Nodes provides a CLI with the `griptape-nodes` (or `gtn`) command. This enables you to interact with Griptape Nodes in the terminal.

`griptape-nodes` (or its shorthand alias `gtn`) is a command-line tool specifically designed to launch and manage the Griptape Nodes Engine installation on your computer. This tool handles tasks like initializing your workspace, managing configuration settings, and starting the engine that powers the web-based Griptape Nodes editor. The actual creation and editing of workflows happens in the web interface that opens when you run the engine.

## Basic Usage

```
griptape-nodes [options] [COMMAND]
```

If no command is specified, the tool defaults to the `engine` command.

## Commands

### `engine` (Default Command)

Run the Griptape Nodes engine.

```
griptape-nodes engine
```

This will start the Griptape Nodes engine and open the web interface at https://nodes.griptape.ai.

### `init`

Initialize a new workspace for Griptape Nodes: sets your API key, workspace
directory, storage backend, and (optionally) extra libraries. Running it
again re-runs the same prompts so you can change any of these later.

```
griptape-nodes init [options]
```

Options:

- `--api-key` - Directly specify your Griptape API key without being prompted
- `--workspace-directory` - Directly specify your workspace directory without being prompted
- `--storage-backend` - Set the storage backend (`local` or `gtc`) without being prompted
- `--bucket-name` - Name for the bucket (existing or new) to use when `--storage-backend gtc` is set
- `--register-diffusers-library` / `--no-register-diffusers-library` - Install (or skip) the Griptape Nodes Diffusers Library
- `--register-griptape-cloud-library` / `--no-register-griptape-cloud-library` - Install (or skip) the Griptape Cloud Library
- `--no-interactive` - Run init without any prompts, using only the flags and defaults provided
- `--hf-token` - Set the Hugging Face token used for downloading gated models
- `--config key=value` - Set an arbitrary configuration value; repeat the flag to set several (e.g. `--config log_level=DEBUG --config workspace_directory=/tmp`)
- `--secret key=value` - Set an arbitrary secret value; repeat the flag to set several (e.g. `--secret MY_API_KEY=abc123`)

### `config`

Manage your Griptape Nodes configuration.

```
griptape-nodes config SUBCOMMAND
```

Subcommands:

- `show [config_path]` - Show the current configuration. With no argument, prints the whole merged configuration as JSON; with a dotted path (e.g. `workspace_directory`), prints just that value
- `list` - List all configuration files that contribute to your configuration, in order of precedence
- `reset` - Reset your configuration to default values

For the full list of settings you can read or set this way, see the
[Configuration Reference](configuration_reference.md).

### `self`

Manage the CLI installation itself.

```
griptape-nodes self SUBCOMMAND
```

Subcommands:

- `uninstall` - Uninstall the CLI, removing its configuration and data directories and the installed executable
- `version` - Display the current version of the CLI
- `info` - Print a system information report for debugging: engine version and install source, platform and Python details, configuration paths, the settings in effect, which API keys are set, every registered library with its version, and any project templates. Safe to paste into a bug report — API keys and other credentials are removed, home directory paths become `~`, and your username becomes `<user>`. A count of what was removed is printed at the end, so a setting that looks empty can be told apart from one that was hidden
    - `--show-identity` - Keep real home directory paths and your username instead of `~` and `<user>`. Credentials are still removed
    - To collect the same information *and* your logs as one shareable file, use [`diagnostics collect`](#diagnostics)

### `libraries`

Manage local libraries. For the full guide on installing and managing
libraries, see [Libraries](../guides/libraries.md).

```
griptape-nodes libraries SUBCOMMAND
```

Subcommands:

- `sync` - Update every registered library to its latest version
    - `--overwrite` - Discard any uncommitted local changes in a library's clone before updating it
- `download <git_url>` - Clone a library from Git and register it
    - `--branch` - Branch, tag, or commit to check out
    - `--target-dir` - Name of the directory to clone into
    - `--download-dir` - Parent directory the library is cloned under
    - `--overwrite` - Overwrite the library's directory if it already exists

### `models`

Manage AI models downloaded from the Hugging Face Hub — the same models
nodes like local diffusion or LLM nodes pull from when you run a workflow.

```
griptape-nodes models SUBCOMMAND
```

Subcommands:

- `download <model_id>` - Download a model from the Hugging Face Hub (e.g. `microsoft/DialoGPT-medium`)
    - `--local-dir` - Local directory to download the model into
    - `--revision` - Git revision to download (defaults to `main`)
- `list` - List all model files currently in your local cache, with their size on disk
- `delete <model_id>` - Delete a model's files from your local cache
- `search [query]` - Search for models on the Hugging Face Hub
    - `--task` - Filter results by task type (e.g. `text-generation`)
    - `--limit` - Maximum number of results to return (defaults to 20, capped at 100)
    - `--sort` - Field to sort results by (defaults to `downloads`)
    - `--direction` - Sort direction (defaults to `desc`)
- `downloads status [model_id]` - Show download progress/status for one model, or for every tracked model if none is given
- `downloads list` - List every tracked model download and its status
- `downloads delete <model_id>` - Delete the download-status tracking record for a model (does not delete the model's files; use `models delete` for that)

### `doctor`

Check your Griptape Nodes installation and print a table of what it found,
with a fix for anything that needs one.

```
griptape-nodes doctor
```

Each check comes back as **PASS**, **WARN** (it works, but something will bite
you later), or **FAIL** (something is broken now). What gets checked:

- **Workspace** - your workspace directory exists and can be written to
- **Disk Space** - there is room on the workspace drive to save workflows and install libraries
- **Libraries** - every registered library loaded, and none of them loaded with missing nodes
- **Secrets** - every API key a library asked for has a value
- **Log Capture** - engine logs are being written, so they can be collected later
- **Cloud Connection** - this machine can reach Griptape Cloud, which the editor needs to talk to the engine

Exits with a nonzero status when a check **fails**, so it's safe to use in
scripts. A warning on its own exits zero.

The same results are saved as `doctor.json` inside a diagnostics bundle, so
whoever reads your bug report sees exactly what you saw.

### `diagnostics`

Collect everything needed to troubleshoot this installation into a single zip
file you can attach to a bug report.

```
griptape-nodes diagnostics SUBCOMMAND
```

Subcommands:

- `collect` - Write a diagnostics bundle: your logs, the settings in effect, every library and project and how it loaded, and the health checks from [`doctor`](#doctor)
    - `--output`, `-o` - Where to write the bundle. A directory gets a generated file name; defaults to the current directory
    - `--skip-logs` - Leave the engine's log files out of the bundle
    - `--skip-libraries` - Do not load libraries first. Faster, but the bundle cannot say which ones fail to load
    - `--show-identity` - Keep real home directory paths and your username instead of `~` and `<user>`

Nothing is uploaded anywhere. The file is written to your machine and it is
yours to share or not.

The bundle is written by the engine, which knows its own API keys, so it finds
and removes them from every file it collects — along with anything else shaped
like a credential. `manifest.json` inside the bundle counts every removal, and
`README.md` explains each file in plain language.

For the editor and desktop equivalents, see
[Exporting engine logs](../troubleshooting.md#exporting-engine-logs).

## Configuration

Griptape Nodes stores its configuration in the following locations.

- Configuration directory: `~/.config/griptape_nodes` (macOS/Linux) or `%USERPROFILE%\.config\griptape_nodes` (Windows)
- Data directory: `~/.local/share/griptape_nodes` (macOS/Linux) or `%USERPROFILE%\.local\share\griptape_nodes` (Windows)
- Configuration file: `griptape_nodes_config.json` in the configuration directory
- Environment file: `.env` in the configuration directory

Griptape Nodes Desktop keeps these two directories inside its own application data folder so they don't collide with a manually installed engine. See [Uninstalling Griptape Nodes](../uninstalling.md#griptape-nodes-desktop) for those paths.

## Workflow

Typical usage flow:

1. Run `griptape-nodes init` to set up your workspace and API key
1. Run `griptape-nodes` to start the engine
1. Use the web interface to create and manage your workflows
