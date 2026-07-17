# Troubleshooting

This page collects the issues and states people run into most often, along with what causes them and how to recover. If you hit something that isn't here, check the [FAQ](faq.md) or reach out through one of the channels at the bottom of the [FAQ](faq.md#where-can-i-provide-feedback-or-ask-questions).

## Images or videos aren't showing in the editor

**Symptoms**

- Load Image, Save Image, or a media preview node shows an empty area instead of the image.
- The file clearly exists on disk (for example in `{outputs}/images/...`), but the editor won't display it.
- Uploading a new image fails with an error like:

    ```
    Error: CreateStaticFileUploadUrl Failed
    Description: Failed to create presigned URL for file ...: Client error
    '404 Not Found' for url 'http://localhost:8124/static-upload-urls'
    ```

**Cause**

Media in the editor is served by a local **static file server** that the engine starts on port `8124`. If that port is already taken, usually by a **second (or stray) Griptape Nodes engine that is still running**, the new engine's static server falls back to a different, OS-assigned port. Media requests then end up split across the two engines, with the stray engine holding the default port while the engine you're actually working with serves from another one, so previews fail to load and uploads fail with `404` errors.

**Fix**

1. Refresh the editor first with Ctrl+R (Windows/Linux) or Cmd+R (macOS). This clears simple display glitches.
1. If media still won't show, make sure **only one engine is running**. Fully quit Griptape Nodes, then look for a leftover engine process:
    - **Windows**: Open Task Manager and look for a stray `Python` process. End it.
    - **macOS / Linux**: In a terminal, run `pgrep -fl griptape` (or look for `python` running the engine) and stop the leftover process.
1. If you can't find or stop the stray process, **restart your computer**. This reliably clears any leftover engine holding the port.
1. Start Griptape Nodes again. On a clean start there should be a single engine process, and media will display normally.

!!! tip

    After restarting, confirm there is only one engine process before reopening your workflow. A single leftover engine from a previous session, especially after an update, is the most common cause of this issue.

!!! note "Running the engine on a remote machine?"

    If your engine runs on a different machine than the editor (or behind a tunnel or reverse proxy), missing media is expected until you point the editor at the right address. Set `static_server_base_url` as described in [Static File Server Configuration](guides/configuration.md#static-file-server-configuration).

## "Address already in use" / the engine won't start

**Symptoms**

You see a startup error similar to:

```
The 'websocket_direct' driver could not start: its address is already in use.
Another Griptape Nodes engine is probably already running.
Stop the other engine (or change this driver's port) and try again.
```

**Cause**

Another Griptape Nodes engine is already running and holding the port this engine needs.

**Fix**

1. Stop the other engine. Quit any other Griptape Nodes windows and check for stray engine processes using the steps in the [media section above](#images-or-videos-arent-showing-in-the-editor).
1. If you intentionally want to run more than one engine on the same machine, see [Running multiple engines on one machine](#running-multiple-engines-on-one-machine).

## Running multiple engines on one machine

**Symptoms**

With two or more engines running on the same machine, you see lots of weird, seemingly unrelated errors: requests answered by the wrong engine (or answered twice), workflows and state crossed between editor sessions, engines that look like one engine in the editor, port errors like the [address-in-use error above](#address-already-in-use-the-engine-wont-start), or media failing to load like the [images issue above](#images-or-videos-arent-showing-in-the-editor).

**Cause**

Two separate problems stack here:

- **Shared identity.** When `GTN_ENGINE_ID` isn't set, every engine launched on the machine resolves to the same default engine identity. Engines sharing an identity listen for the same requests and share the same session state, so they both try to answer requests meant for one of them. This is what produces the flood of strange errors.
- **Port conflicts.** The first engine takes the default ports (such as `8124` for the static file server); later engines fall back to other ports, which breaks anything still pointing at the defaults.

**Fix**

Give every additional engine **its own identity and its own ports**:

```bash
GTN_ENGINE_ID=second-engine STATIC_SERVER_PORT=9000 GTN_MCP_SERVER_PORT=9928 gtn engine
```

If you never intended to run more than one engine, find and stop the extra one using the steps in the [media section above](#images-or-videos-arent-showing-in-the-editor).

## "No sessions available" — the engine won't start for license users

**Symptoms**

You activated with a license (rather than logging in through Griptape Cloud), and on launch the engine fails license allocation with an error like `No sessions available`.

**Cause**

Your organization has a fixed pool of license sessions (seats). A seat is held for as long as an engine is running and is released when the engine shuts down cleanly. `No sessions available` means every seat in the pool is currently held — either legitimately (everyone is using theirs) or by a **stale session**: an engine that crashed, was force-killed, or is still running orphaned in the background keeps holding (and renewing) its seat.

**Fix**

1. Check for an orphaned engine on your own machine, especially after a crash or a force-quit, using the steps in the [media section above](#images-or-videos-arent-showing-in-the-editor). Stopping it releases your seat.
1. If a seat is stuck, an organization owner can release it: in the [Admin Dashboard](enterprise/admin_dashboard.md#sessions), open the **Sessions** modal and **Release** the stale session to free the seat.
1. Otherwise, a stale session frees itself once it expires — seats time out when they stop being renewed, so waiting a few minutes and trying again also works.

!!! note

    A related error, `No session pool configured`, means your organization isn't set up with license sessions at all — contact whoever administers your Griptape Nodes licenses.

## The editor is black or blank

**Symptoms**

The editor window goes black or blank, often after the machine has been idle or asleep, or after a brief network interruption.

**Fix**

- Refresh the editor with Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (macOS). A hard refresh reloads the editor and reconnects to the engine.

## Libraries or nodes are missing, or you see errors from another engine

**Symptoms**

- No libraries show up, or a node you expect (such as the Agent node) is gone.
- The editor shows errors that reference a different engine or workflow than the one you're looking at.

**Cause**

Usually one of two things:

- **Something prevented a library from loading.** When a library fails to load (a missing dependency, a broken node file, an import error, and so on), its nodes silently won't appear. **The logs are the source of truth here.** Export or open the engine logs and look for errors around library loading at startup.
- **The Libraries To Register setting isn't what you think.** The engine only loads the libraries listed in the **Libraries To Register** setting (**Configuration Editor → Libraries → Library Registration**, stored as `app_events.on_app_initialization_complete.libraries_to_register` in `griptape_nodes_config.json`). If a library isn't in that list, is toggled off, or its entry is stale, its nodes won't show up.
- You may also simply be connected to a different engine than you think, and it surfaces that engine's libraries and errors.

**Fix**

1. **Check the logs first.** Look for errors emitted while libraries load on startup. The reported error usually names the library and the reason it failed. See [Exporting engine logs](#exporting-engine-logs).
1. Confirm which engine the editor is connected to. If you have engines on multiple machines, the editor may have connected to the wrong one.
1. Open the **Configuration Editor**, go to the **Libraries** view, and check **Library Registration → Libraries To Register**. If the library you expect is missing, toggled off, or points somewhere stale, fix the entry, or re-add the library via **Manage → Library Management → Add Library**. See [Toggling and removing libraries](guides/libraries.md#toggling-and-removing-libraries) and [Installing a library](guides/editor/managing_models_and_libraries.md#installing-a-library).
1. Check the **Libraries** panel with the filter set to **Errors** for libraries that failed to install or load. See ["I installed the library but I don't see its nodes"](guides/libraries.md#i-installed-the-library-but-i-dont-see-its-nodes).
1. Make sure your libraries are up to date. Open **Manage → Library Management**, expand the library, and click **Check for Updates**, then **Update** when one is offered. See [Updating a library](guides/editor/managing_models_and_libraries.md#updating-a-library). To update the engine itself, see the [FAQ](faq.md#how-do-i-update-griptape-nodes).

## Exporting engine logs

When you report an issue (or dig into one yourself), the engine logs are usually the first thing to look at. Where to get them depends on how you run the engine.

### From the desktop application

The desktop application keeps its own log files for the local engine it manages, and can export logs for a time range, not just the current session. This is especially useful when the problem happened a while ago or spans an engine restart.

1. Click **Engine** in the header (the button that shows the engine status) to open the engine popover.
1. Under **Managed Engine**, click **Logs** to open the engine logs window.
1. Click **Export**.
1. In the **Export Logs** dialog, choose:
    - **Current Engine Session** — logs since the engine was last started.
    - **Time Range** — logs between specific timestamps, with a **From** time and either a **To** time or a **Now** checkbox. When an issue just happened, exporting the last half hour or so is usually more useful than the whole session.
1. Choose where to save the `.txt` file.

!!! note

    Exporting requires the **Write engine logs to file** setting, found in the desktop application's Settings. It is enabled by default; if the **Export** button is disabled, use the **Manage** link next to it to jump to that setting.

### From the terminal

If you run the engine manually (with `gtn` or `gtn engine`), logs print directly to that terminal. Scroll back and copy the relevant portion from there.

If the logs don't show enough detail, raise the engine's log level: open the Configuration Editor (**Settings → All Settings**), search for "log level", set it to `DEBUG`, and reproduce the issue (see [Editing Settings in the Editor](guides/configuration.md#editing-settings-in-the-editor)). When running headless with no editor attached, you can set it through an environment variable instead:

```bash
GTN_CONFIG_LOG_LEVEL=DEBUG gtn
```

## Related error-specific entries

A few specific error messages are documented in the FAQ:

- [`failed to locate pyvenv.cfg`](faq.md#im-seeing-failed-to-locate-pyvenvcfg-the-system-cannot-find-the-file-specified-what-should-i-do)
- [`Attempted to create a Flow with a parent 'None'`](faq.md#im-seeing-attempted-to-create-a-flow-with-a-parent-none-but-no-parent-with-that-name-could-be-found-what-should-i-do)
- [`ssl.SSLCertVerificationError`](faq.md#im-receiving-an-error-when-trying-to-run-griptape-nodes-sslsslcertverificationerror-ssl-certificate_verify_failed-certificate-verify-failed-self-signed-certificate-in-certificate-chain-_sslc1000-what-should-i-do)
