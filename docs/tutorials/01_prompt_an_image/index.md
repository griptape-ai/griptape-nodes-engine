# Lesson 2: Prompt an Image

Welcome to the second tutorial in our Griptape Nodes series! This guide focuses on getting familiar with the GenerateImage node.

While the workflow itself is exceedingly simple (consisting of just this single node), the GenerateImage node will be your primary workhorse for image generation in your early projects. This powerful node truly is the cornerstone for creating a wide variety of visual content.

## What we'll cover

In this tutorial, you will:

- Learn how to open saved workflows
- Learn about the GenerateImage node
- Generate images using text prompts

## Navigate to the Landing Page

To begin this tutorial, you'll need to return to the main landing page. Click on the navigation element at the top left of the editor to go back to where all the template workflows and your own saved files are displayed.

<p align="center">
  <img src="assets/nav_bar.png" alt="Nav Bar" width="300">
</p>

## Open the Image Prompt Example

On the landing page, locate and click on the **"Prompt an Image"** tile to open this example workflow.

<p align="center">
  <img src="assets/prompt_image_example.png" alt="Prompt an Image example">
</p>

## Understand the GenerateImage Node

When the example loads, you'll notice it consists of just a single node. Don't be fooled by its simplicity – this node is one of the most powerful tools in Griptape Nodes and will likely feature prominently in your future flows.

<p align="center">
  <img src="assets/generate_image_node.png" alt="GenerateImage node" width="300">
</p>

This node has been configured to handle many tasks that would typically require a more complex flow, making it perfect for getting started with AI image generation.

## Generate Images Using Text Prompts

The primary point of interaction for this node is the text prompt field where you describe what image you want the AI to create.

To generate your first image:

1. Locate the text prompt field in the node
1. Type a description for the image you want to create

<p align="center">
    <img src="assets/text_prompt_field.png" alt="Text prompt field" width="300">
  </p>

Now, run your node. There are three UI buttons but those perform only two distinct operations:

<p align="center">
  <img src="assets/ways_to_run.png" alt="Ways to run" width="300">
</p>

1. Run the *whole* workflow:

    1. Click the **Run Workflow** button at the top of the editor. This executes the entire workflow from start to finish. All nodes will be processed in sequence according to their connections.

1. Run a single node in the workflow (two methods that do the same thing):

    1. Click the **Run Node** button in the top right corner of a specific node.
    1. Or, select a node and click **Run Selected** from the toolbar.

Running a single node can be very useful for testing or debugging specific parts of your workflow, or simply getting things to run faster if you don't care about updating other parts of the workflow just yet.

The difference is in scope - the first option runs everything, while the second options run just the selected node and everything that precedes it that it may need.

!!! warning "Generation times"

    For those new to image generation: generation operations can take time. Please notice the yellow outlines and spinning **Running** icon on nodes, where the **run node** icon was before execution began. These visual hints tell you which nodes are currently resolving, letting you know that things are functioning as expected.

    <p align="center">
      <img src="assets/node_running.gif" alt="Node running" width="300">
    </p>

## Experiment with Different Descriptions

Let's try generating some images with different prompts:

1. **First Example**: The workflow loads with "A potato making an oil painting" in the prompt field. Run the flow

    <p align="center">
    <img src="assets/potato_painting.png" alt="Potato painting result" width="300">
    </p>

1. **Second Example**: Change the prompt to "A potato doing aerobics in 70s workout attire" and run the flow again

    <p align="center">
    <img src="assets/potato_aerobics.png" alt="Potato aerobics result" width="300">
    </p>

Notice how dramatically different the results are just by changing a few words in your prompt. This demonstrates the flexibility and power of the GenerateImage node. Anything you can describe, you can generate.

## Summary

In this tutorial, we covered:

- How to open saved workflows
- The GenerateImage node
- How to generate images using text prompts

The GenerateImage node is a fundamental building block for creative flows in Griptape Nodes. As you progress, you'll discover how to combine it with other nodes to develop even more powerful applications.

## Next Up

In the next section: [Lesson 3: Coordinating Agents](../02_coordinating_agents/index.md), we'll learn how to get AIs to team up and form a bucket-brigade through a workflow.
