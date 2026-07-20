# Frequently Asked Questions

!!! tip

    Chasing a specific problem or error? See [Troubleshooting](troubleshooting.md) for commonly encountered issues and how to recover from them.

## Where is my workspace (where do my files save)?

Files such as saved workflows, etc., are saved in a the Workspace Directory.

The path for the Workspace Directory can be found in the Griptape Nodes Editor:

1. Open the Griptape Nodes Editor.
1. Open an existing workflow or create a blank one.
1. Click "Settings".
1. Select "Configuration Editor".
1. Click "Griptape Nodes Settings" on the leftmost column, if not already selected.
1. The path is listed under "Workspace Directory".

If you are not running the Editor, run this command and it will report back your Workspace Directory:

```bash
gtn config show | grep workspace
```

## Can I run the Engine on a different machine than the Editor?

The Engine and Editor can run on completely separate machines. Remember that any files you save or libraries you register will be stored on the machine where the Engine is running. So if you're looking for your files and can't find them right away, double-check which machine the Engine is running on.

## Where is Griptape Nodes installed?

Looking for the exact installation location of your Griptape Nodes? This command will show you precisely where it's installed:

=== "macOS / Linux"

    ```bash
    dirname $(dirname $(readlink -f $(which griptape-nodes)))
    ```

=== "Windows (PowerShell)"

    ```powershell
    Split-Path (Split-Path (Get-Command griptape-nodes).Source)
    ```

## Can I see or edit my config file?

To get a path to the file, go to the top Settings menu in the Editor, and select **Copy Path to Settings**. That will copy the config file path to your clipboard.

If you prefer working in the command line, you can also use:

```
gtn config show
```

## How do I install the Advanced Media Library after Initial Setup?

For the broader story on installing and managing libraries (including non-Advanced-Media libraries), see [Libraries](guides/libraries.md).

If you initially declined to install the Advanced Media Library during setup but now want to add it, you can do so by running:

```bash
gtn init
```

This will restart the configuration process. You can press Enter to keep your existing workspace and Griptape Cloud API Key settings. When prompted with:

```
Register Advanced Media Library? [y/n] (n):
```

Press **y** to install the Advanced Media Library, or **n** to skip installation.

!!! note

    Some nodes in the Advanced Media Library require specific models to function properly. You will need to install these models separately.

    Refer to each node's documentation to determine which nodes need which models; they each have links to specific requirements.

## What happened to deprecated nodes from the Advanced Media Library?

Version 0.64.0 removed deprecated nodes from the Advanced Media Library. These nodes were previously marked for deprecation and have been replaced with more flexible alternatives.

If you have workflows that use deprecated nodes, please refer to the [MIGRATION.md](https://github.com/griptape-ai/griptape-nodes/blob/main/MIGRATION.md) guide. This comprehensive guide provides:

- A complete list of removed nodes and their replacements
- Step-by-step migration instructions
- Visual examples of replacement nodes
- Details about the new Diffusion Pipeline Builder system

The migration guide includes replacements for all deprecated image processing, diffusion pipeline, upscaling, and LoRA nodes.

## How do I uninstall Griptape Nodes?

```bash
griptape-nodes self uninstall
```

To reinstall, follow the instructions on the [installation](installation.md) page.

## How do I update Griptape Nodes?

Griptape Nodes will automatically check if it needs to update every time it runs. If it does, you will be prompted to answer with a (y/n) response. Respond with a y and it will automatically update to the latest version of the Engine.

If you would like to _manually_ update, you can always use either of these commands:

```bash
griptape-nodes self update
griptape-nodes libraries sync
```

or

```bash
gtn self update
gtn libraries sync
```

## Where can I provide feedback or ask questions?

You can connect with us through several channels:

- [Website](https://www.griptape.ai) - Visit our homepage for general information
- [Discord](https://discord.gg/gnWRz88eym) - Join our community for questions and discussions
- [GitHub](https://github.com/griptape-ai/griptape-nodes) - Submit issues or contribute to the codebase

These same links are also available as the three icons in the footer (bottom right) of every documentation page.

## How can I test out unreleased features?

If you're interested in testing out unreleased features, you can install the pre-release builds of Griptape Nodes.
Updates are now published to the [latest](https://github.com/griptape-ai/griptape-nodes/releases/tag/latest) tag twice a day.

!!! warning

    Pre-release builds are not guaranteed to be stable and may contain bugs or incomplete features. Use them at your own risk.

To switch to the pre-release update channel, run the following commands:

```
uv tool uninstall griptape-nodes
uv tool install git+https://github.com/griptape-ai/griptape-nodes.git@latest --reinstall --force --python 3.12
```

This will uninstall the current version of Griptape Nodes and install the latest pre-release build from the GitHub repository.

!!! info

    Uninstalling using `uv tool uninstall griptape-nodes` will not remove your existing projects or settings. It only removes the Griptape Nodes engine itself.

You can confirm it went through by running `gtn self version`. Your version number should show a reference to a git commit:

```
gtn self version
v0.31.0 (git - e172e80)
```

To switch back to the stable release channel, run the following commands:

```
uv tool uninstall griptape-nodes
uv tool install griptape-nodes
```
