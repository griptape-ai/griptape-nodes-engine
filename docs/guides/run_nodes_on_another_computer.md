# Running Nodes on Another Computer

You can keep working in the editor on your own computer while a library's nodes run on a different computer, such as one with a more powerful GPU. When you run a workflow, those nodes run over there, and their results, progress, logs, and previews show up in your editor as if they had run on your computer.

This page calls your computer the **workstation** and the other one the **remote computer**. Each one runs its own Griptape Nodes engine.

## How it works

Some libraries run their nodes in a separate process called a **worker**. Normally your engine starts that worker on your workstation. When you send a library to a remote computer, your engine asks the remote engine to start the worker there instead. The worker then reports back to your engine, so your workflow runs the same way it always does.

Every engine has an **engine ID**, a long string like `3f2c8e1a-5b7d-4c9e-a0f1-2b3c4d5e6f70`. You tell your engine which libraries to send where by pairing each library's name with the remote engine's ID. Your engine looks that ID up in the **engine registry**, a list of running engines that Griptape Cloud keeps, or that your studio's admin server keeps. The `GT_CLOUD_BASE_URL` setting says which one an engine uses. Because the lookup is by ID, you never type the remote computer's address.

## Before you start

Check each of these. If one doesn't hold, running the library's nodes fails with a message that says what is wrong.

- **Same version of Griptape Nodes.** Both computers run the same engine version.
- **Same libraries.** The remote computer has each library you send it installed and loaded, at the same version as your workstation.
- **A shared workspace.** Both computers open the same workspace folder, for example on a shared network drive. Files never travel between the engines; each one reads and writes the shared folder.
- **The same engine registry.** Both engines use the same `GT_CLOUD_BASE_URL`.
- **Credentials on the remote computer.** The remote computer has its own Griptape Nodes license or Griptape Cloud API key, in `GRIPTAPE_NODES_LICENSE` or `GT_CLOUD_API_KEY`. Nodes that run there use it, just as the remote engine does. Your workstation doesn't send its license or API key.
- **A way to connect.** Either your workstation can reach the remote computer over your network, or both engines connect to Griptape Cloud. See step 2.

## Step 1: Find the remote engine's ID

On the remote computer, run:

```bash
gtn self info
```

Copy the value after `Engine ID:`.

The ID stays the same when the engine restarts. It changes only if the engine's data directory is deleted, or if `GTN_ENGINE_ID` is set to a different ID.

## Step 2: Make the remote engine reachable

Your workstation picks the connection from how the remote engine is set up:

- If the remote engine sets `advertise_url`, your workstation connects to it directly over your network.
- Otherwise, it connects through Griptape Cloud.

It doesn't switch between the two. If a direct connection fails, the node fails and says so; it doesn't try Griptape Cloud instead.

### Direct over your network

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

### Through Griptape Cloud

Use this when your workstation can't reach the remote computer directly, for example because it's on another network.

- Leave `advertise_url` unset on the remote computer.
- Make sure the `websocket_api` transport is on for both engines. See [Transports](transports.md).
- Connect both engines to Griptape Cloud with the same Griptape Cloud API key, such as your organization's.

Connecting through Griptape Cloud with a Griptape Nodes license isn't available yet. With a license, connect directly.

## Step 3: Send a library to the remote computer

On your **workstation**, add the library to `distributed_execution.library_engines` in your `griptape_nodes_config.json`, paired with the remote engine's ID from step 1:

```json
{
  "distributed_execution": {
    "library_engines": {
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

A change to `distributed_execution.library_engines` takes effect the next time that library's worker starts, for example after you reload the library.

## Sharing one remote computer between people

Several people can send their libraries to the same remote computer. It runs one person's run at a time, so two people's nodes never share its GPU at once.

- **Runs take turns, in the order they asked.** When your run's first node arrives while someone else's run has the remote computer, your node waits. Meanwhile the run toolbar in your editor shows your run as queued, with its place in line.
- **A run keeps the remote computer until it ends.** Once your run has it, your run's later nodes there start right away, even while someone else waits. It passes to the next run when yours ends, including when you cancel it. A paused run keeps the remote computer until you continue it to the end or cancel it. If your engine stops or loses its connection to the remote computer, the remote computer passes to the next run.
- **Don't send libraries both ways between two computers.** If computer A sends a library to B and B sends one to A, a run on each can wait for the other forever: each holds its own computer while it waits for the other's. Cancel one run to free both. The remote computer's log names the other engine when this happens.
- **The previous person's models are freed first.** When the remote computer passes from one person's run to another's, it frees models the way its `distributed_execution.release` setting says:

| Setting         | Value                      | What happens                                                                                                                    |
| --------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `release.which` | `others` (the default)     | Frees everyone's workers except those of the person whose run is starting, so your models stay loaded between your own runs.    |
| `release.which` | `all`                      | Frees everyone's workers, yours included.                                                                                       |
| `release.which` | `none`                     | Frees nothing. For a remote computer that runs one pipeline.                                                                    |
| `release.how`   | `drop_cache` (the default) | Each worker drops what its libraries keep between runs, such as a loaded model. Values a later node of the same run needs stay. |
| `release.how`   | `recycle`                  | Stops each worker. It starts again before that library's next node, which takes as long as loading the library.                 |

To limit how many workers the remote computer runs at once, set `distributed_execution.max_workers` on it. A start beyond the limit is refused, and the node that needed it says so. Your next node from that library asks again. For example, on the **remote computer**:

```json
{
  "distributed_execution": {
    "max_workers": 4,
    "release": { "which": "others", "how": "drop_cache" }
  }
}
```

## Good to know

- **Only some libraries can run elsewhere.** A library can run on another computer only if its nodes run in a worker, which is up to the library's author. Libraries with heavy dependencies, such as machine learning models, usually do. If you list one that doesn't, nothing fails: its nodes keep running on your workstation.
- **Secrets come from your workstation.** When a node asks for a secret by name, such as a provider API key, it gets your workstation's value.
- **Only use a remote computer you trust.** Your nodes running there act for your workstation: they can read your secrets and your workspace files, and work as your engine does. So you're trusting the remote computer, anyone who can connect to its Griptape Nodes port, and anyone whose nodes run on it, such as other people sharing it. Keep it on a network only your team reaches.
- **If the remote computer moves to a new address**, restart Griptape Nodes on your workstation so it looks the remote engine up again.
- **You can share one config file between computers.** A library listed with an engine's own ID runs on that engine. So computers that share a workspace can share its `griptape_nodes_config.json`: the remote computer runs its libraries itself, and your workstation sends them there.

## Troubleshooting

Each message below is how the error starts, or a part of it you'll find in the node's error.

| What the error says                                                                 | What to do                                                                                                                                                                           |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Failed because the engine registry has no engine with that ID                       | Check the engine ID in `distributed_execution.library_engines` against `gtn self info` on the remote computer, and that both engines use the same `GT_CLOUD_BASE_URL`.               |
| Engine ... is not running: it stopped reporting to the engine registry              | Start Griptape Nodes on the remote computer.                                                                                                                                         |
| Both engines must run the same engine version                                       | Update Griptape Nodes so both computers run the same version.                                                                                                                        |
| the library is not installed on it                                                  | Install the library on the remote computer.                                                                                                                                          |
| Both engines must have the same version                                             | Install the same version of the library on both computers.                                                                                                                           |
| the library is not loaded on it                                                     | Open the remote engine's Libraries list, and enable or fix the library.                                                                                                              |
| cannot be reached from this engine: it publishes no address for a direct connection | Set `advertise_url` on the remote computer, or connect both engines to Griptape Cloud. The end of the message says which side isn't connected to Griptape Cloud.                     |
| advertise_url must look like ws://host:port, with no path                           | Fix the remote computer's `advertise_url`.                                                                                                                                           |
| Failed to connect                                                                   | Check that `advertise_url` names an address your workstation can reach, and that the remote computer's firewall allows the port.                                                     |
| did not answer within ... seconds                                                   | Check that the remote engine is running and reachable. If the message adds that both engines must connect as the same identity, use the same Griptape Cloud API key on both engines. |
| stopped while starting up                                                           | The remote engine's log has the details, often a dependency that failed to install.                                                                                                  |
| the most its distributed_execution.max_workers setting allows                       | Raise `distributed_execution.max_workers` on the remote computer, or wait until fewer people use it.                                                                                 |
