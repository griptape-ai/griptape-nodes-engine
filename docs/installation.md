# Installing Griptape Nodes

There are two ways to get Griptape Nodes running:

- **[Griptape Nodes Desktop](#griptape-nodes-desktop-recommended)** (recommended) — a fully managed desktop application that bundles the engine and the editor. It handles sign-in, provisions an API key for you, and keeps everything up to date. No terminal required.
- **[Manual engine install](#advanced-manual-engine-install)** — for power users who want to install and run the engine themselves from the command line, paired with the web-based editor.

If your organization gave you a **license key** instead of a Griptape account, see [Activating with a license](#activating-with-a-license) below.

## Griptape Nodes Desktop (Recommended)

Griptape Nodes Desktop is a single application that includes both the engine and the editor. It automatically installs and manages the engine, signs you in, and provisions a Griptape API key on your behalf — there is nothing to configure by hand.

### 1. Download and install

Download Griptape Nodes Desktop for your platform from [griptapenodes.com](https://griptapenodes.com). It is available for macOS, Windows, and Linux.

### 2. Sign in

Launch the application and click **Login or Sign-Up** to sign in with your Griptape account.

<!-- screenshot: the desktop app login screen showing the Login or Sign-Up button and the Activate with a License button -->

> If you've already signed up for [Griptape Cloud](https://cloud.griptape.ai), your existing credentials will work here!

If your organization issued you a license key instead of a Griptape account, click **Activate with a License** instead — see [Activating with a license](#activating-with-a-license).

### 3. Choose your workspace

On first run, you'll be asked to choose a *workspace directory*. Your workspace directory is where Griptape Nodes saves your [project files](./glossary.md#project-files) and [generated assets](./glossary.md#generated-assets). Accept the default or pick any location you prefer.

<!-- screenshot: the desktop app first-run workspace setup step with the default workspace directory shown -->

That's it! The application takes care of the rest — installing the engine, generating an API key, and opening the editor. You're ready to move on to the [tutorials](tutorials/index.md).

## Advanced: Manual Engine Install

Prefer to manage the engine yourself? With this approach, you install the engine from the command line and use the editor in your web browser at [https://nodes.griptape.ai](https://nodes.griptape.ai).

The Editor and the Engine are decoupled and communicate with each other through an event service, so the engine doesn't have to run on the same machine as your browser. If you want the engine to have access to more resources than your laptop provides, you can run it on a separate machine — the instructions below work the same for either approach.

### 1. Sign up or log in

To get started, visit [https://griptapenodes.com](https://griptapenodes.com) and click the sign-in button.

> If you've already signed up for [Griptape Cloud](https://cloud.griptape.ai), your existing credentials will work here!

Once you've logged in, the editor opens with an engine setup dialog (it appears automatically whenever no engine is connected). Select the **Get Started** tab and expand **Install Engine Manually** to follow along with the steps below.

<!-- screenshot: the engine setup dialog on the Get Started tab, showing the Griptape Nodes Desktop download card and the Install Engine Manually accordion expanded -->

### 2. Install the engine

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you don't already have it.

1. Run the following command to install the Griptape Nodes Engine:

    ```bash
    uv tool install griptape-nodes
    ```

After installation, run `griptape-nodes` (or the shorthand `gtn`) in the terminal *for the first time* and you'll be walked through a series of configuration questions.

### 3. Configuration

**First**, you'll be prompted for your Griptape API Key. This key allows the engine to communicate with the editor.

1. Return to the editor tab in your web browser. In the engine setup dialog, under the **Get Started** tab's **Install Engine Manually** section, open the **Generate an API Key** step.

1. Click the **Generate API Key** button. Your new key appears in place of the button.

1. Copy the key and paste it into the terminal prompt. The key is only displayed once in the browser, so copy it before moving on.

<!-- screenshot: the Generate an API Key step in the Install Engine Manually section, with a generated key shown in place of the button -->

```
╭─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Griptape API Key                                                                                                        │
│         A Griptape API Key is needed to proceed.                                                                        │
│         This key allows the Griptape Nodes Engine to communicate with the Griptape Nodes Editor.                        │
│         In order to get your key, return to the https://nodes.griptape.ai tab in your browser and click the button      │
│         "Generate API Key".                                                                                             │
│         Once the key is generated, copy and paste its value here to proceed.                                            │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Griptape API Key (YOUR-KEY-HERE):
```

!!! info

    If you've previously run `gtn init` your key might be presented to you in this dialog. You can accept it by pressing Enter or use a different value as required.

**Second**, you'll be prompted to set your *workspace directory*. Your workspace directory is where the engine will save [project files](./glossary.md#project-files) and [generated assets](./glossary.md#generated-assets). It will also contain a [.env](./glossary.md#.env) for your Griptape Nodes [secret keys](./glossary.md#secret-keys).

```
╭───────────────────────────────────────────────────────────────────╮
│ Workspace Directory                                               │
│     Select the workspace directory. This is the location where    │
│     Griptape Nodes will store your saved workflows.               │
│     You may enter a custom directory or press Return to accept    │
│     the default workspace directory                               │
╰───────────────────────────────────────────────────────────────────╯
Workspace Directory (/Users/user/Documents/GriptapeNodes)
```

Pressing Enter will use the default: `<current_working_directory>/GriptapeNodes`, where `<current_working_directory>` is the directory from which you're running the `gtn` command. Alternatively, you can specify any location you prefer.

**Finally**, you'll be offered a few optional configuration steps. Each can be skipped by accepting the default, and all of them can be revisited later:

- **Storage backend** — store static files locally (the default) or in a Griptape Cloud bucket.
- **Griptape Cloud bucket** — used for syncing workflows and assets across multiple machines.
- **Hugging Face token** — needed only if you plan to download gated models from the Hugging Face Hub.
- **Additional libraries** — optionally install the Diffusers and Griptape Cloud node libraries.

> You can always return to this configuration flow using the `gtn init` command if you need to make changes in the future.

### 4. Start your engine

Your installation is now complete and you're ready to proceed to creating your first workflow or trying out one of the sample workflows. To get started, run `griptape-nodes` (or `gtn`) in your terminal, then return to your browser. Your browser tab at [https://nodes.griptape.ai](https://nodes.griptape.ai) will update to show *Create from scratch*, allowing you to start from a blank canvas, together with several sample Griptape Nodes workflows that you can experiment with!

<!-- screenshot: the editor after the engine connects, showing Create from scratch and the sample workflows -->

## Activating with a License

Organizations can run Griptape Nodes on license keys instead of individual Griptape accounts. If your organization admin sent you a license key, you don't need to sign up for anything:

1. Install [Griptape Nodes Desktop](#griptape-nodes-desktop-recommended) as described above.
1. On the login screen, click **Activate with a License** instead of signing in.
1. Paste your license key and activate.

If your organization runs an on-premises [Admin Server](enterprise/admin_server.md), you'll also point the application at it during activation. See [Using the Admin Server](enterprise/using_the_admin_server.md) for the full walkthrough of the license activation flow.

Organization admins looking to issue and manage license keys should start with the [Admin Dashboard](enterprise/admin_dashboard.md) guide.

## Next Steps

Next, on to learning how to actually work inside Griptape Nodes! [Begin](tutorials/index.md)
