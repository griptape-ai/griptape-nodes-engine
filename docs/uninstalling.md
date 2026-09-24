# Uninstalling Griptape Nodes

Start with the section that matches how you installed:

- **[Griptape Nodes Desktop](#griptape-nodes-desktop)** — the desktop application you downloaded from [griptapenodes.com](https://griptapenodes.com).
- **[Manual engine install](#manual-engine-install)** — the engine you installed yourself with `uv tool install griptape-nodes`.

Then finish with [What's left behind](#whats-left-behind).

!!! warning "Your workflows are not removed for you"

    No uninstaller touches your workspace directory, so your projects, workflows, and generated assets stay on disk until you delete them yourself. Copy anything you want to keep somewhere safe *before* you get to [Remove your workspace](#remove-your-workspace).

## Griptape Nodes Desktop

The application bundles the engine and manages the engine's directories inside its own data folder.

### macOS

1. Quit Griptape Nodes.
1. Open **Applications** in Finder and drag **Griptape Nodes** to the Trash.
1. Empty the Trash.

This leaves the data folder in place. See [Remove the application's data](#remove-the-applications-data-macos-and-linux).

### Windows

1. Quit Griptape Nodes.
1. Open **Settings → Apps → Installed apps**.
1. Find **Griptape Nodes**, open its **...** menu, and choose **Uninstall**.

The Windows uninstaller removes:

- The application, from `%LOCALAPPDATA%\ai.griptape.nodes.desktop`, including any downloaded update packages kept alongside it.
- Start menu and desktop shortcuts.
- The **Installed apps** entry.
- Both of the application's data folders, `%APPDATA%\Griptape Nodes` and `%LOCALAPPDATA%\Griptape Nodes`. That covers the app's settings and logs plus everything the engine wrote, including your API keys.

Continue with [What's left behind](#whats-left-behind).

### Linux

Griptape Nodes Desktop ships as an AppImage: a single file that isn't installed anywhere in particular.

1. Quit Griptape Nodes.
1. Delete the `.AppImage` file you downloaded.
1. If you registered the AppImage with a helper such as AppImageLauncher, remove it through that tool instead so the launcher entry under `~/.local/share/applications` goes with it.

Then continue with [Remove the application's data](#remove-the-applications-data-macos-and-linux).

### Remove the application's data (macOS and Linux)

On macOS and Linux, deleting the application leaves its data behind. Delete these folders too. On Windows the uninstaller already removed the equivalents.

| What                       | macOS                                                 | Linux                           |
| -------------------------- | ----------------------------------------------------- | ------------------------------- |
| Application data           | `~/Library/Application Support/Griptape Nodes`        | `~/.config/Griptape Nodes`      |
| Log files                  | `~/Library/Logs/Griptape Nodes`                       | `~/.config/Griptape Nodes/logs` |
| Downloaded update packages | `~/Library/Caches/velopack/ai.griptape.nodes.desktop` | —                               |

The application data folder holds app settings, your sign-in session, any license keys you activated, and the files the engine wrote: `griptape_nodes_config.json`, the `.env` file with your API keys and other secrets, agent conversation threads, model download records, and the ffmpeg binaries the engine downloads for video previews. Your node libraries are not in here; they live in your workspace, which is [handled separately](#remove-your-workspace). On Linux, updates are staged in a temporary directory, so there is nothing to clean up there.

!!! tip "Find the exact path from inside the app"

    Before you uninstall, open **App Settings** and scroll to [App Data Locations](guides/desktop/app_settings.md#app-data-locations). It shows the application data folder for your machine, with an **Open** button to reveal it in Finder or your file manager. Log files and update packages aren't listed there, so use the table above for those.

Then continue with [What's left behind](#whats-left-behind).

## Manual engine install

Run the uninstaller:

```bash
gtn self uninstall
```

Press Enter when it prints `When done, press Enter to exit.` so it can finish removing the executable. It removes:

- The engine's **configuration** and **data** directories, `~/.config/griptape_nodes` and `~/.local/share/griptape_nodes`, including the `.env` file with your API keys and other secrets.
- The `griptape-nodes` and `gtn` commands, by running `uv tool uninstall griptape-nodes`.

If it prints a **Caveats** section, read it. It lists the configuration files it left alone, the ones inside your workspace and project folders, along with anything it couldn't delete and you should remove by hand.

!!! note "Where those folders are on Windows"

    The engine uses the same layout on every platform, so on Windows the two folders are `%USERPROFILE%\.config\griptape_nodes` and `%USERPROFILE%\.local\share\griptape_nodes`. Both sit under your home folder, not in `AppData`.

If `gtn` no longer runs, usually because a previous uninstall left a broken virtual environment behind, do the same work by hand:

```bash
uv tool uninstall griptape-nodes
rm -rf ~/.config/griptape_nodes ~/.local/share/griptape_nodes
```

On Windows, in PowerShell:

```powershell
uv tool uninstall griptape-nodes
Remove-Item -Recurse -Force "$env:USERPROFILE\.config\griptape_nodes"
Remove-Item -Recurse -Force "$env:USERPROFILE\.local\share\griptape_nodes"
```

Then continue with [What's left behind](#whats-left-behind).

## What's left behind

### Remove your workspace

Your [workspace directory](guides/projects/workspace.md) holds your projects, workflows, generated assets, and node libraries, and is never deleted automatically. It defaults to:

- `Documents/GriptapeNodes` if you used the desktop application.
- A `GriptapeNodes` folder inside whichever directory you first ran `gtn` from, if you installed the engine yourself.

You may have pointed it somewhere else, so note the path before you uninstall.

Copy out anything you want to keep, then delete the folder.

### Remove downloaded models

Models that nodes pull from the Hugging Face Hub go into the shared Hugging Face cache, not into a Griptape Nodes directory.

| Platform      | Path                                   |
| ------------- | -------------------------------------- |
| macOS / Linux | `~/.cache/huggingface/hub`             |
| Windows       | `%USERPROFILE%\.cache\huggingface\hub` |

If anything else on your machine downloads from Hugging Face, don't delete the whole folder. With a manual engine install you can clear out just the Griptape Nodes models before you uninstall:

```bash
gtn models list
gtn models delete <model_id>
```

You can also do this from the editor's **Model Management** window while Griptape Nodes is still installed. See [Managing models and libraries](guides/editor/managing_models_and_libraries.md).

### Remove uv

Griptape Nodes uses [uv](https://docs.astral.sh/uv/) to build the virtual environments for node libraries, and uv keeps caches and Python interpreters of its own. If uv is on your machine only because you installed Griptape Nodes, follow uv's [uninstallation instructions](https://docs.astral.sh/uv/getting-started/installation/#uninstallation) to remove those along with uv itself.

Those instructions leave your `PATH` alone. uv's installer added its executable directory (`~/.local/bin`, or `%USERPROFILE%\.local\bin` on Windows) to it, so once uv is gone you can drop that entry: on macOS and Linux delete the line uv added to your shell profile, and on Windows remove it under **Settings → System → About → Advanced system settings → Environment Variables**.

## Reinstalling

To come back, follow [Installing Griptape Nodes](installation.md) again. If you kept your workspace, point the new install at it and your projects and workflows are where you left them.
