# Custom (OpenAI-compatible)

Use this provider type to connect Griptape Nodes to any endpoint that implements the OpenAI Chat Completions API — whether that's a third-party service, a self-hosted model server, or a corporate proxy.

## Prerequisites

You'll need:

- The **base URL** of the endpoint (e.g., `https://api.openai.com/v1`)
- An **API key** for that endpoint
- The **model name** to use (e.g., `google/gemma-4-e4b`) — required because custom endpoints don't support automatic model discovery

## Add the Provider

1. Open **Settings → Agent Settings**
1. Click **+ Add Provider**
1. Select **Custom (OpenAI-compatible)**

<!-- TODO(#5095): screenshot of the "Add Provider — Configure Custom (OpenAI-compatible)" step -->

Fill in the configuration:

- **Name** — a label to identify this provider in the model dropdown

- **Icon** — choose an icon to represent this provider (optional)

- **Base URL** — the root URL of the endpoint

- **API Key Secret** — select an existing secret, or click **+** to create one:

    <!-- TODO(#5095): screenshot of the "Create Secret" modal -->

    - **Secret Name** — the environment variable name (e.g., `OPENAI_API_KEY`)
    - **Value** — the actual key value

    After saving, select the new secret from the dropdown.

- **Model** — enter the model name to use with this endpoint

Click **Create Provider**.

## Test

After creation the wizard shows a confirmation screen. Verify you get a response before clicking **Done**.

<!-- TODO(#5095): screenshot of the "Provider Added" confirmation step -->

## Related

- [AI Providers overview](./index.md)
- [Ollama](./ollama.md) — local provider, no API key needed
- [LM Studio](./lm_studio.md) — local provider, no API key needed
