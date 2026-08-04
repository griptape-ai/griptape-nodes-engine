# LoadImage

## What is it?

The LoadImage is a simple building block that lets you bring an image into your workflow. Think of it as picking up a photo so you can use it in your project.

## When would I use it?

Use this node when you want to:

- Use an image that was created by another node (like CreateImage)
- Pass an image to other nodes in your workflow
- Display an image as part of your project

## How to use it

### Basic Setup

1. Add the LoadImage to your workflow
1. Connect it to a source of images (like a CreateImage)

### Parameters

- **image**: The image to load (this can be connected to an output from another node)

### Outputs

- **image**: The loaded image that can be used by other nodes in your flow

## Example

Imagine you've created an image with the CreateImage and now want to use it elsewhere:

1. Connect the "output" from your CreateImage to the "image" input of the LoadImage
1. The LoadImage will make the image available to use in the rest of your workflow

## Important Notes

- The LoadImage simply passes the image through - it doesn't change the image itself
- You can click the file browser icon to select an image from your computer
- The image preview can be expanded by clicking the expander icon

## Common Issues

- **No Image Showing**: Make sure you've properly connected an image source to this node
- **Wrong Image Type**: Make sure you're connecting an ImageUrlArtifact, ImageArtifact, or string path/URL to this node
