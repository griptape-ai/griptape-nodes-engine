# Ollama (local)

[Ollama](https://ollama.com) lets you run AI models locally on your own machine. No API key is required and no data leaves your computer. Griptape Nodes dynamically discovers which models you have installed.

## Prerequisites

1. Download and install Ollama from [ollama.com](https://ollama.com)

1. Pull at least one model. For example:

    ```bash
    ollama pull llama3.2
    ```

1. Verify Ollama is running and the model is available:

    ```bash
    ollama list
    ```

    Ollama must be running whenever you want to use it in Griptape Nodes.

## Add the Provider

1. Open **Settings → Agent Settings**
1. Click **+ Add Provider**
1. Select **Ollama (local)**

<!-- TODO(#5095): screenshot of the "Add Provider — Configure Ollama (local)" step -->

Fill in the configuration:

- **Name** — leave as `Ollama` or enter a custom label
- **Base URL** — defaults to `http://localhost:11434/v1`; click the refresh button to test the connection
- When Ollama is running you'll see **Connected — N models available** in green
- **Model (optional)** — choose a default model, or leave blank to select at chat time

Click **Create Provider**.

## Test

After creation the wizard shows a confirmation screen. Select a model, type a test message, and verify you get a response before clicking **Done**.

<!-- TODO(#5095): screenshot of the "Provider Added" step with a successful test response -->

## Choosing Models

Models you've pulled with `ollama pull` appear automatically in:

- The **Model** dropdown in the [Agent](../index.md), grouped under the Ollama provider name
- The `prompt model` parameter on the **Agent node** when `provider` is set to Ollama

To add more models, run `ollama pull <model-name>` in a terminal and they'll appear immediately.

## Related

- [AI Providers overview](./index.md)
- [LM Studio](./lm_studio.md) — another local provider option
- [Local Models with Agents (MCP approach)](../../mcp/advanced_local_models.md) — an alternative pattern using Ollama via MCP nodes in a workflow
