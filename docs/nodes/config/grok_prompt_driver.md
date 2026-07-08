# GrokPrompt

!!! warning "Billing Required for xAI API Usage"

    The GrokPrompt node requires an xAI account and billing information to be set up before xAI API keys will work. Without completing the billing setup, any nodes using xAI will fail, even with a valid API key. See [this guide](../../guides/integrations/grok.md) for instructions on setting up a xAI account with billing.

## What is it?

The GrokPrompt node sets up a connection to xAI's Grok models.

## When would I use it?

Use this node when you want to:

- Use Grok's chat models in your workflow
- Customize how your agents interact with Grok models
- Control specific settings for Grok model responses

## How to use it

### Basic Setup

1. Add a GrokPrompt node to your workflow
1. Connect its output to nodes that can use Grok prompt models (for instance, an Agent!)

### Parameters

- **model**: The model to use. Default is "grok-3-beta", choices are "grok-3-beta", "grok-3-fast-beta", "grok-3-mini-beta", "grok-3-mini-fast-beta", "grok-2-vision-1212"
- **top_p**: Controls diversity of outputs (default: 0.9)
- **stream**: Whether to receive responses as they're generated (true) or all at once (false)
- **temperature**: Controls randomness in responses (higher values = more creative, lower = more focused)
- **max_attempts_on_fail**: How many times to retry if there's an error
- **use_native_tools**: Whether to use Anthropic's built-in tools
- **max_tokens**: Maximum length of responses

### Outputs

- **prompt_model_config**: The configured Grok driver that other nodes can use

## Example

To create an agent that uses Grok models with specific settings:

1. Add a GrokPrompt to your workflow
1. Connect the **prompt_model_config** output to an Agent's **prompt_model_config** input
1. Now that agent will use Grok with your custom settings

Things to try:

1. Try changing the "model" from "grok-3-beta" to "grok-3-mini-beta"
1. Set "top_p" to 0.7 (for more focused responses)

## Important Notes

- You need a valid Grok API key set up in your environment as `GROK_API_KEY`
- Unlike some other drivers, Grok doesn't support parameters like 'seed' and 'top_k'
- Grok is a paid service, and you must have billing set up on the Grok.ai website before your API key will work
