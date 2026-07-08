# AI Providers

Griptape Nodes supports multiple AI providers for the [chat sidebar](../agent_sidebar.md) and the **Agent node** in workflows. Griptape Cloud is provided by default. You can add local providers or any OpenAI-compatible endpoint as additional options.

<!-- TODO(#5095): screenshot of Agent Settings → AI Providers section showing the provider list and "+ Add Provider" button -->

## Available Providers

| Provider                                  | Type                   | API Key Required |
| ----------------------------------------- | ---------------------- | ---------------- |
| [Griptape Cloud](./griptape_cloud.md)     | Hosted proxy (default) | Griptape API key |
| [Ollama](./ollama.md)                     | Local                  | No               |
| [LM Studio](./lm_studio.md)               | Local                  | No               |
| [Custom (OpenAI-compatible)](./custom.md) | Hosted or local        | Yes              |

## Adding a Provider

Open **Settings → Agent Settings**. In the **AI Providers** section, click **+ Add Provider**.

<!-- TODO(#5095): screenshot of the "Add Provider — Choose type" modal -->

Select the provider type you want to add and follow the configuration steps on its page (linked in the table above). The wizard walks you through three steps: choose type → configure → test.

## Managing Providers

Each provider (other than Griptape Cloud) has three controls:

| Control        | Action                                                                    |
| -------------- | ------------------------------------------------------------------------- |
| Toggle         | Enable or disable. Disabled providers don't appear in the model dropdown. |
| Edit (pencil)  | Update name, base URL, API key, or default model                          |
| Delete (trash) | Remove the provider                                                       |

<!-- TODO(#5095): screenshot showing the toggle, edit, and delete controls on a provider row -->

## Using a Provider in the Chat Sidebar

Once a provider is enabled, its models appear in the **Model** dropdown in the chat sidebar, grouped by provider name. Click any model to switch to it immediately.

See [Agent Sidebar](../agent_sidebar.md) for more on the chat interface.

## Using a Provider in a Workflow

The **Agent node** has a `provider` parameter. Set it to any enabled provider to use it for that node's AI calls. The `prompt model` parameter lets you pick the specific model.

<!-- TODO(#5095): screenshot of the Agent node showing the provider and prompt model parameters -->

This lets different nodes in the same workflow use different providers.
