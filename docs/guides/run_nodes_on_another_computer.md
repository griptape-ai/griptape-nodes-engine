# Running Nodes on Another Computer

You can keep working in the editor on your own computer while a library's nodes run on a different computer, such as one with a more powerful GPU. When you run a workflow, those nodes run over there, and their results, progress, logs, and previews show up in your editor as if they had run on your computer.

This page calls your computer the **workstation** and the other one the **remote computer**. Each one runs its own Griptape Nodes engine.

## How it works

Some libraries run their nodes in a separate process called a **worker**. Normally your engine starts that worker on your workstation. When you send a library to a remote computer, your engine asks the remote engine to start the worker there instead. The worker then reports back to your engine, so your workflow runs the same way it always does.

Every engine has an **engine ID**, a long string like `3f2c8e1a-5b7d-4c9e-a0f1-2b3c4d5e6f70`. You tell your engine which libraries to send where by pairing each library's name with the remote engine's ID. Your engine looks that ID up in the **engine registry**, a list of running engines that Griptape Cloud keeps, or that your studio's admin server keeps. The `GT_CLOUD_BASE_URL` setting says which one an engine uses. Because the lookup is by ID, you never type the remote computer's address.

## Before you start

Check each of these. If one doesn't hold, the node fails with a message saying which.

- **Same version of Griptape Nodes.** Both computers run the same engine version.
- **Same libraries.** The remote computer has each library you send it installed and loaded, at the same version as your workstation.
- **The library runs in a worker.** Only a library that declares execution dependencies runs its nodes in a worker, and only those can run on another computer. If you list a library that doesn't, its nodes keep running on your workstation.
- **A shared workspace.** Both computers open the same workspace folder, for example on a shared network drive. Files never travel between the engines; each one reads and writes the shared folder.
- **The same engine registry.** Both engines use the same `GT_CLOUD_BASE_URL`.
- **Credentials on the remote computer.** The remote computer has its own Griptape Nodes license or Griptape Cloud API key, in `GRIPTAPE_NODES_LICENSE` or `GT_CLOUD_API_KEY`. Nodes that run there use it, just as the remote engine does. Your workstation doesn't send its license or API key.
- **A way to connect.** Either your workstation can reach the remote computer over your network, or both engines connect to Griptape Cloud. See step 2.

## Step 1: Find the remote engine's ID

On the remote computer, open `engines.json` in the engine's data directory and copy the `id` of the engine. The data directory is `~/.local/share/griptape_nodes` on macOS and Linux and `%USERPROFILE%\.local\share\griptape_nodes` on Windows, unless `GTN_ENGINE_DATA_DIR` points somewhere else.

The ID stays the same when the engine restarts. It changes only if that data directory is deleted, or if `GTN_ENGINE_ID` is set to a different ID.

## Step 2: Make the remote engine reachable

Your workstation connects to the remote engine in one of two ways, and picks the way itself.

### Option A: Connect over your network

Use this when your workstation can reach the remote computer directly. It works without Griptape Cloud, and the traffic stays on your network.

On the **remote computer**, have the engine listen on an address other computers can reach, and publish that address in `advertise_url`:

```json
{
  "transports": {
    "websocket_direct": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 18125,
      "advertise_url": "ws://render-01:18125"
    }
  }
}
```

- Replace `render-01` with the remote computer's hostname or IP address, as your workstation would reach it.
- `advertise_url` must start with `ws://` and include the port. It can't have a path.
- Allow incoming connections on that port in the remote computer's firewall.

For more about these settings, see [Transports](transports.md).

### Option B: Connect through Griptape Cloud

Use this when your workstation can't reach the remote computer directly, for example because it's on another network.

- Leave `advertise_url` unset on the remote computer.
- Make sure the `websocket_api` transport is on for both engines. See [Transports](transports.md).
- Connect both engines to Griptape Cloud with the same Griptape Cloud API key, such as your organization's.

Connecting through Griptape Cloud with a Griptape Nodes license isn't available yet. With a license, use Option A.

!!! note

    If the remote engine publishes an `advertise_url`, your workstation always connects directly. If that connection fails, the node fails and says so. It doesn't fall back to Griptape Cloud.

## Step 3: Send a library to the remote computer

On your **workstation**, add the library to `distributed_execution.libraries` in your `griptape_nodes_config.json`, paired with the remote engine's ID from step 1:

```json
{
  "distributed_execution": {
    "libraries": {
      "Griptape Nodes Diffusers Library": "3f2c8e1a-5b7d-4c9e-a0f1-2b3c4d5e6f70"
    }
  }
}
```

- Use the library's name exactly as it appears in the editor's Libraries list. Names are case-sensitive.
- Set this in the config file, not in an environment variable. Environment variable names are lowercased, which would change the library's name.
- You can list several libraries, each with its own engine ID. Libraries you don't list run on your workstation as usual.

## Step 4: Load the library and run your workflow

When your engine loads the library, it asks the remote computer to start the library's worker. That can take a while if the library has large dependencies to load. If you run one of its nodes before the worker is ready, the node waits for it, up to `worker.library_load_timeout_s`.

Then run your workflow as you normally would. The library's nodes run on the remote computer, and their results come back to your editor.

A change to `distributed_execution.libraries` takes effect the next time that library's worker starts, for example after you reload the library.

## Good to know

- **One person per remote engine, for now.** Sharing one remote engine between several people isn't supported yet. If two people send work to the same engine, their nodes run there at the same time.
- **Secrets come from your workstation.** When a node asks for a secret by name, such as a provider API key, it gets your workstation's value.
- **If the remote computer moves to a new address**, restart Griptape Nodes on your workstation so it looks the remote engine up again.
- **Two computers can't each run a library for the other.** If your workstation connects to the remote computer, the remote computer can't also send its own libraries to your workstation over that connection.
- **Listing your own engine's ID does nothing special.** The library runs on your workstation, as if it weren't listed. That happens, for example, when computers that share a workspace share its config file.

## Troubleshooting

| What the node says                                                      | What to do                                                                                                                                                          |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Both engines must run the same engine version                           | Update Griptape Nodes so both computers run the same version.                                                                                                       |
| The library is not installed on it, or has a different version          | Install the same version of the library on the remote computer.                                                                                                     |
| The library is not loaded on it                                         | Open the remote engine's Libraries list and fix or enable the library.                                                                                              |
| It publishes no address for a direct connection, and ...                | Set `advertise_url` on the remote computer (Option A), or connect both engines to Griptape Cloud (Option B).                                                        |
| `advertise_url` must look like `ws://host:port`, with no path           | Fix the remote computer's `advertise_url`.                                                                                                                          |
| Could not connect, or did not answer in time                            | Check that the remote engine is running, that `advertise_url` names an address your workstation can reach, and that the remote computer's firewall allows the port. |
| Through Griptape Cloud, both machines must connect as the same identity | Use the same Griptape Cloud API key on both engines.                                                                                                                |
| The worker stopped while starting up                                    | The remote engine's log has the details, often a dependency that failed to install.                                                                                 |
| Nodes still run on your workstation                                     | Check that the library runs in a worker, and that its name in `distributed_execution.libraries` matches exactly.                                                    |
