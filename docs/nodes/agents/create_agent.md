# Agent

## What is it?

The Agent node lets you configure an AI Agent with customizable capabilities like tools and rulesets. This node can create an Agent for immediate use given it's own prompt, or can be passed to other nodes' "agent" inputs.

## When would I use it?

Use this node when you want to:

- Create a configurable AI Agent from scratch
- Set up an Agent with specific tools and rulesets
- Prepare an Agent that can be reused across your workflow
- Get immediate responses from your agent using a custom prompt

## How to use it

### Basic Setup

1. Add the Agent to your workflow
1. Configure the agent's capabilities (tools and rulesets)

### Parameters

- **agent**: An existing Agent configuration (optional). If specified, it will use the existing Agent when prompting.
- **provider**: The AI provider to use (e.g., Griptape Cloud, Ollama, LM Studio, or a custom endpoint). See [AI Providers](../../how_to/ai_providers/index.md) for setup instructions.
- **prompt model**: The specific model from the selected provider.
- **prompt**: The instructions or question you want to ask the Agent.
- **additional_context**: String or key-value pairs providing additional context to the Agent.
- **tools**: Capabilities you want to give your Agent.
- **rulesets**: Rules that tell your Agent what it can and cannot do.
- **output_schema**: A JSON Schema template that defines the exact format you want the Agent's response to follow (optional).

### Outputs

- **output**: The text response from your agent (if a prompt was provided)
- **agent**: The configured agent object, which can be connected to other nodes

## Example

Imagine you want to create an Agent that can write haikus based on prompt_context:

1. Add a KeyValuePair
1. Set the "key" to "topic" and "value" to "swimming"
1. Add an Agent
1. Set the Agent "prompt" to "Write me a haiku about {{topic}}"
1. Connect the KeyValuePair dictionary output to the Agent "prompt_context" input
1. Run the workflow
1. The Agent "output" will contain a haiku about swimming!

## Using Output Schemas

### What is an Output Schema?

Think of an output schema as a form you're asking the AI to fill out. Instead of getting a free-form text response, you can specify exactly what pieces of information you want and how they should be organized.

For example, instead of asking "Tell me about this product" and getting a paragraph of text, you can ask for:

- A product name (text)
- A price (number)
- Whether it's in stock (yes/no)
- A list of features (multiple text items)

The AI will then respond with structured data that matches exactly what you asked for.

### Why Use an Output Schema?

Use output schemas when you need:

- **Consistent formats**: Every response follows the same structure, making it easier to process
- **Specific data types**: Guarantee you get numbers where you need numbers, lists where you need lists, etc.
- **Easier automation**: Structured data is much easier to connect to other nodes in your workflow
- **Validation**: The AI must provide all required fields in the correct format

### How to Create an Output Schema

1. Add a **JSON Input** node to your workflow
1. Add a JSON Schema that defines the output that you want (you can use online tools to help create this)

### Output Schema Example

Let's say you want to extract information about a restaurant from a review:

1. Create Schema Fields:

    - Field "restaurant_name" (type: string)
    - Field "rating" (type: integer)
    - Field "price_range" (type: string)
    - Field "cuisine_type" (type: string)
    - Field "recommended_dishes" (type: list, list_type: string)

1. Connect all fields to a Create Schema node

1. Connect the schema to your Agent's output_schema input

1. Set your Agent's prompt: "Extract restaurant information from this review: [review text]"

1. The Agent will now respond with structured data instead of plain text, containing exactly those fields

1. The generated schema will look like this:

```json
{
  "type": "object",
  "properties": {
    "restaurant_name": { "type": "string" },
    "rating": { "type": "integer" },
    "price_range": { "type": "string" },
    "cuisine_type": { "type": "string" },
    "recommended_dishes": {
      "type": "array",
      "items": { "type": "string" }
    }
  },
  "required": ["restaurant_name", "rating", "price_range", "cuisine_type", "recommended_dishes"]
}
```

### What Changes When Using a Schema?

- **Output type**: The Agent's output changes from plain text to structured data (JSON format)
- **Validation**: If the AI cannot provide data in the requested format, it will try again or return an error

## Important Notes

- If you don't provide a prompt, the node will create the agent without running it and the output will contain exactly "Agent Created"
- The node supports both streaming and non-streaming prompt drivers
- Tools and rulesets can be provided as individual items or as lists
- The additional_context parameter allows you to provide additional_context to the agent as a string or dictionary of key/value pairs
- By default the node uses Griptape Cloud, which requires a valid `GT_CLOUD_API_KEY`. You can switch to a local or custom provider using the **provider** parameter — see [AI Providers](../../how_to/ai_providers/index.md).
- When you pass an Agent from one node to another using the agent input/output pins, the conversation memory is maintained, which means:
    - The Agent "remembers" previous interactions in the same flow
    - Context from previous prompts influences how the Agent interprets new prompts
    - You can build multi-turn conversations across multiple nodes
    - The Agent can reference information provided in earlier steps of your workflow
- Don't know how to create a JSON Schema for the output_schema parameter? Use an online tool like [JSON Schema Builder](https://transform.tools/json-to-json-schema) to create one based on your desired output structure. Or just give an Agent an example of the output you want and have it generate the schema for you!

## Common Issues

- **No provider configured**: If no provider is set, the node uses Griptape Cloud by default, which requires a valid `GT_CLOUD_API_KEY`. See [AI Providers](../../how_to/ai_providers/index.md) to add Ollama, LM Studio, or a custom endpoint.
- **Streaming Issues**: If using a streaming prompt driver, ensure your flow supports handling streamed outputs.
