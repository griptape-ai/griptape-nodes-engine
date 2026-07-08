# LM Studio (local)

[LM Studio](https://lmstudio.ai) is a desktop app for downloading and running AI models locally. No API key is required and no data leaves your computer. Griptape Nodes dynamically discovers which models you have loaded.

## Prerequisites

1. Download and install LM Studio from [lmstudio.ai](https://lmstudio.ai)

1. Inside LM Studio, download at least one model using the model browser

1. Start the **Local Server** inside LM Studio — Griptape Nodes connects to this server, so it must be running whenever you want to use LM Studio as a provider

<!-- TODO(#5095): screenshot of LM Studio showing the Local Server running -->

## Add the Provider

1. Open **Settings → Agent Settings**
1. Click **+ Add Provider**
1. Select **LM Studio (local)**

<!-- TODO(#5095): screenshot of the "Add Provider — Configure LM Studio (local)" step -->

Fill in the configuration:

- **Name** — leave as `LM Studio` or enter a custom label
- **Base URL** — defaults to `http://localhost:1234/v1`; click the refresh button to test the connection
- When the LM Studio server is running you'll see **Connected — N models available** in green
- **Model (optional)** — choose a default model, or leave blank to select at chat time

Click **Create Provider**.

## Test

After creation the wizard shows a confirmation screen. Select a model, type a test message, and verify you get a response before clicking **Done**.

<!-- TODO(#5095): screenshot of the "Provider Added" step with a successful test response -->

## Choosing Models

Models loaded in LM Studio appear automatically in:

- The **Model** dropdown in the [Agent](../index.md), grouped under the LM Studio provider name
- The `prompt model` parameter on the **Agent node** when `provider` is set to LM Studio

To add more models, download them inside LM Studio and they'll appear immediately.

## Related

- [AI Providers overview](./index.md)
- [Ollama](./ollama.md) — another local provider option
