# GenerateImage

## What is it?

The GenerateImage node generates images based on text prompts using Griptape Cloud services. It integrates with Griptape Cloud Image Generation Driver and Prompt Driver to transform textual descriptions into high-quality images.

## When would I use it?

Use the GenerateImage node when:

- You need to generate images dynamically in your workflow
- You want to create visual content based on text descriptions
- You need to visualize concepts described in natural language

## How to use it

### Basic Setup

1. Add a GenerateImage node to your workflow
1. Provide a text prompt describing the image you want to generate
1. Run your workflow!

### Parameters

- **agent**: The agent responsible for handling prompt and image generation tasks (Agent or dict)
- **image_generation_driver**: The driver used for image generation (defaults to None)
- **prompt**: Text description of the image to generate (string)
- **enhance_prompt**: Whether to enhance the prompt for better image quality (boolean, defaults to True)

### Outputs

- **output**: The generated image as an ImageUrlArtifact

## Example

A simple workflow to generate and save an image:

1. Add a GenerateImage node to your workflow
1. Connect an Agent node into the agent parameter
1. Set the prompt to "A serene mountain landscape at sunset with a lake reflecting the orange sky"
1. Connect the output to a SaveImage node to store the generated image

## Important Notes

- You must set the `GT_CLOUD_API_KEY` environment variable for authentication with Griptape Cloud
- The node uses 'dall-e-3' as the default model and 'hd' as the default quality
- Enhanced prompts can improve image quality but may interpret your prompt differently than expected
