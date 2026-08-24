# Uninstalling Griptape Nodes

Start with the section that matches how you installed:

- **[Griptape Nodes Desktop](#griptape-nodes-desktop)** — the desktop application you downloaded from [griptapenodes.com](https://griptapenodes.com).
- **[Manual engine install](#manual-engine-install)** — the engine you installed yourself with `uv tool install griptape-nodes`.

Then finish with [What's left behind](#whats-left-behind).

!!! warning "Your workflows are not removed for you"

    Nothing an uninstaller does touches your workspace directory, so your projects, workflows, and generated assets survive. That also means they are still there after you think you're done. Copy anything you want to keep somewhere safe *before* you get to [Remove your workspace](#remove-your-workspace).

## Before you start

1. **Quit Griptape Nodes.** In the desktop application, quit the app and let it stop the engine. If you run the engine in a terminal, stop it with Ctrl+C. Uninstalling while the engine is running can leave files locked, which is a common cause of a half-finished uninstall on Windows.

1. **Write down your workspace directory.** You'll want it later, and the tools that can tell you where it is are about to be removed.

    - Desktop application: open **App Settings** and read **Workspace Directory** under [Workspace & Libraries](guides/desktop/app_settings.md#workspace-libraries).
    - Manual install: run `gtn config show workspace_directory`.

1. **Export your logs if you plan to file a bug.** Uninstalling deletes them. See [Exporting engine logs](troubleshooting.md#exporting-engine-logs).

## Griptape Nodes Desktop

The application bundles the engine and manages the engine's directories inside its own data folder.

### macOS

1. Quit Griptape Nodes.
1. Open **Applications** in Finder and drag **Griptape Nodes** to the Trash.
1. Empty the Trash.

That's the application and the engine inside it. Its data folder stays behind — see [Remove the application's data](#remove-the-applications-data-macos-and-linux).

### Windows

1. Quit Griptape Nodes.
1. Open **Settings → Apps → Installed apps**.
1. Find **Griptape Nodes**, open its **...** menu, and choose **Uninstall**.

The Windows uninstaller cleans up more than the other platforms do. It removes:

- The application, from `%LOCALAPPDATA%\ai.griptape.nodes.desktop`, including any downloaded update packages kept alongside it.
- Start menu and desktop shortcuts.
- The **Installed apps** entry itself.
- Both of the application's data folders, `%APPDATA%\Griptape Nodes` and `%LOCALAPPDATA%\Griptape Nodes`. Griptape Nodes hooks into the uninstaller to delete these for you, so the app's settings and logs, and everything the engine wrote including your API keys, go with them.

It does not remove your workspace or downloaded models. Skip ahead to [What's left behind](#whats-left-behind).

### Linux

Griptape Nodes Desktop ships as an AppImage: a single file that isn't installed anywhere in particular.

1. Quit Griptape Nodes.
1. Delete the `.AppImage` file you downloaded.
1. If you registered the AppImage with a helper such as AppImageLauncher, remove it through that tool instead, so it also removes the launcher entry it created under `~/.local/share/applications`.

Then continue with [Remove the application's data](#remove-the-applications-data-macos-and-linux).

### Remove the application's data (macOS and Linux)

On macOS and Linux, deleting the application leaves its data behind. Delete these folders to finish the job. (On Windows the uninstaller already removed the equivalents.)

| What                       | macOS                                                 | Linux                           |
| -------------------------- | ----------------------------------------------------- | ------------------------------- |
| Application data           | `~/Library/Application Support/Griptape Nodes`        | `~/.config/Griptape Nodes`      |
| Log files                  | `~/Library/Logs/Griptape Nodes`                       | `~/.config/Griptape Nodes/logs` |
| Downloaded update packages | `~/Library/Caches/velopack/ai.griptape.nodes.desktop` | —                               |

The application data folder is the big one. It holds app settings, your sign-in session, any license keys you activated, and the files the engine wrote: `griptape_nodes_config.json`, the `.env` file with your API keys and other secrets, agent conversation threads, model download records, and the ffmpeg binaries the engine downloads for video previews. Your node libraries are not in here: they live in your workspace, which is [handled separately](#remove-your-workspace). On Linux, updates are staged in a temporary directory rather than a folder of the app's own, so there is nothing to clean up there.

!!! tip "Find the exact path from inside the app"

    Before you uninstall, open **App Settings** and scroll to [App Data Locations](guides/desktop/app_settings.md#app-data-locations). It shows the application data folder for your machine, with an **Open** button to reveal it in Finder or your file manager. Log files and update packages aren't listed there, so use the table above for those.

Once those are gone, continue with [What's left behind](#whats-left-behind).

## Manual engine install

Run the uninstaller:

```bash
gtn self uninstall
```

Press Enter when it prints `When done, press Enter to exit.` so it can finish removing the executable. It removes:

- The engine's **configuration** and **data** directories, `~/.config/griptape_nodes` and `~/.local/share/griptape_nodes`, including the `.env` file with your API keys and other secrets.
- The `griptape-nodes` and `gtn` commands, by running `uv tool uninstall griptape-nodes`.

If it prints a **Caveats** section, read it. It lists configuration files it deliberately left alone, which are the ones inside your workspace and project folders, along with anything it couldn't delete and you should remove by hand.

!!! note "Where those folders are on Windows"

    The engine uses the same layout on every platform, so on Windows the two folders are `%USERPROFILE%\.config\griptape_nodes` and `%USERPROFILE%\.local\share\griptape_nodes` — under your home folder, not in `AppData`.

If `gtn` won't run anymore, which happens when a previous uninstall left a broken virtual environment behind, do the same work by hand:

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

Your [workspace directory](guides/projects/workspace.md) is where your work lives, so it is never deleted automatically. It defaults to:

- `Documents/GriptapeNodes` if you used the desktop application.
- A `GriptapeNodes` folder inside whichever directory you first ran `gtn` from, if you installed the engine yourself.

Either way you may have pointed it somewhere else, which is why it's worth [noting the path before you uninstall](#before-you-start).

Copy out anything you want to keep, then delete the folder.

### Remove downloaded models

Models that nodes pull from the Hugging Face Hub go into the shared Hugging Face cache, not into anything Griptape Nodes owns.

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

Griptape Nodes uses [uv](https://docs.astral.sh/uv/) to build the virtual environments for node libraries, so uv keeps caches and Python interpreters of its own. If uv is on your machine only because you installed Griptape Nodes, follow uv's [uninstallation instructions](https://docs.astral.sh/uv/getting-started/installation/#uninstallation) to remove those along with uv itself.

Those instructions leave your `PATH` alone. Installing Griptape Nodes put uv's executable directory (`~/.local/bin`, or `%USERPROFILE%\.local\bin` on Windows) on it, so once uv is gone you can drop that entry too: on macOS and Linux delete the line uv added to your shell profile, and on Windows remove it under **Settings → System → About → Advanced system settings → Environment Variables**.

If you never had uv before installing Griptape Nodes, the install script's private copy of it is already gone with the data directory, but uv's cache is not. Delete `~/.cache/uv` (`%LOCALAPPDATA%\uv\cache` on Windows) by hand.

## Reinstalling

Nothing here blocks a future install. To come back, follow [Installing Griptape Nodes](installation.md) again. If you kept your workspace, point the new install at it and your projects and workflows are waiting where you left them.
