# Lesson 1: Getting Started

Welcome to Griptape Nodes! This tutorial will guide you through the basics of this powerful visual workflow tool. You'll learn how to launch the application, navigate the interface, add and connect nodes, and run your first AI agent. By the end of this guide, you'll have the foundational knowledge needed to continue with subsequent tutorials.

## What We'll Cover

In this tutorial, topics include:

- Launching Griptape Nodes
- Navigating through the landing page to a workflow
- Getting familiar with the Griptape Nodes Editor
- Adding nodes to the workspace
- Learning what can connect to what
- Run an agent

## Launch Griptape Nodes

To launch Griptape Nodes, open your terminal and run one of the following commands:

```bash
griptape-nodes
```

Or use the shorter version:

```bash
gtn
```

After executing the command, your browser should automatically open to [https://nodes.griptape.ai](https://nodes.griptape.ai). If it doesn't, simply ctrl-click (or cmd-click on Mac) the link displayed in your terminal and select "Open in browser" if prompted. Of course, you can always simply bookmark it in your browser, and come back to it that way.

<p align="center">
  <a href="https://nodes.griptape.ai">
    <img src="assets/launch_link.png" alt="Griptape Nodes launch link in terminal">
  </a>
</p>

!!! tip

    For the best experience, keep two browser windows open side-by-side: this tutorial in one, and your Griptape Nodes session in the other.

## The Landing Page

When your browser opens, you'll be greeted by the Griptape Nodes landing page. This page displays several template workflows that showcase different things we want to introduce you to. Once you start saving your own workflows, they will appear here in order of newest-to-oldest.

<p align="center">
  <img src="assets/landing_page.png" alt="Griptape Nodes landing page">
</p>

These sample workflows are excellent resources for learning about Griptape Nodes' capabilities, but for now, let's start "from scratch".

## Create a new workflow from scratch

On the landing page, locate and click on the **"Create from scratch"** tile. This action opens a blank workspace where you can build workflows.

<p align="center">
  <img src="assets/create_from_scratch.png" alt="Create from scratch option">
</p>

## Get familiar with the Griptape Nodes interface

Once you're in the Workflow Editor, take a moment to familiarize yourself with the interface:

<p align="center">
  <img src="assets/workspace_interface.png" alt="Griptape Nodes workspace interface">
</p>

### Libraries

The most important area to focus on initially is the left panel, the node library. At the top, you'll find the **Create Nodes** section. This panel houses all the standard nodes that come pre-packaged with Griptape Nodes. Each node serves a specific function. As you become familiar with Griptape Nodes, you'll learn how these nodes work and how to combine them to create powerful automations.

<p align="center">
  <img src="assets/create_nodes_panel.png" alt="Create Nodes panel" width=250">
</p>

## Adding Nodes to the Workspace

There are three interactive methods to creating nodes (and even more in [Retained Mode](../../development/retained_mode.md))

<div style="display: flex; justify-content: space-between; gap: 20px; margin-bottom: 30px;">
  <div style="flex: 1;">
    <p><strong>Drag and Drop</strong>: Click and hold on a node from the left panel, then drag it onto your workspace.</p>
    <p align="center">
      <img src="assets/create_node_dragDrop.gif" alt="Drag and Drop">
    </p>
    <h4 align="center">Drag and Drop</h4>
  </div>

<div style="flex: 1;">
    <p><strong>Double-Click</strong>: Simply double-click any node in the left panel to automatically place it in the center of your workspace.</p>
    <p align="center">
      <img src="assets/create_node_dblClick.gif" alt="Double Click">
    </p>
    <h4 align="center">Double Click</h4>
  </div>

<div style="flex: 1;">
    <p><strong>Shift+A or Double-Click</strong>: Pressing Shift+A or double-clicking on the flow editor brings up a search field. You can type to find the node you want, and enter to create it.</p>
    <p align="center">
      <img src="assets/create_node_spacebar.gif" alt="Shift+A or Double-Click">
    </p>
    <h4 align="center">Shift+A or Double-Click Search</h4>
  </div>
</div>

After adding a node, you can:

- Click and drag to reposition it on the workspace
- Edit its values and behaviors
- Connect it to other nodes

## Connecting Nodes

Let's create some nodes using the first technique mentioned above - dragging and dropping a node from the library to the workspace to create three nodes.

1. An **Agent** ( agents > Agent )
    \- This is an agent that interacts with LLMs (Like OpenAI ChatGPT or Anthropic Claude)

    1. Open the agents category in the sidebar
    1. Drag the Agent node to the workspace and release to create it

    !!! info

        For brevity, we'll describe this as ( category > Node ), so for an Agent, we'd shorthand the above with ( agents > Agent ). Try the same process with the next two nodes.

1. A **FloatInput** ( number > FloatInput )
    \- A node to input decimal numbers (floats)

1. A **TextInput** ( text > TextInput )
    \- A node to input text

<p align="center">
  <img src="assets/nodes_in_workspace.png" alt="Node on the workspace">
</p>

Experiment with connections by dragging from ports on both input nodes to various ports on the Agent. Try multiple combinations and observe that not all connections succeed.

This happens because parameters can only connect directly when their data types are compatible:

- The **TextInput** node outputs **text**, so it can connect to any Agent parameter that accepts text.
- The **FloatInput** node outputs decimal numbers (**floats**), which can't connect to any parameter on the Agent.

Don't worry that you can't connect the FloatInput to anything - that's exactly the point. This node was included _here_ solely to demonstrate how not all parameters can connect to each other. The FloatInput node is indeed very useful, it's just not one we can use with the other nodes currently in _this_ workflow.

!!! Pro tip "Pro Tip"

    Use the port colors as a visual guide for compatibility. Ports with matching colors can connect to each other.

<p align="center">
  <img src="assets/connected.png" alt="Node on the workspace">
</p>

## Use an Agent

For now, lets try another method to wipe the slate clean, and get a real AI interaction under our belt:

1. Go to the File menu and choose **New**.

1. In your new workflow, make an Agent any way you prefer

1. Type a question into the agent's prompt field. You can use "Who trained you?" to verify the AI service, or simply enter any question you'd normally ask a chatbot.

    <p align="center">
    <img src="assets/eg_prompt.png" alt="Example prompt" width="300">
    </p>

1. Click the play button icon in the top right corner of the agent to run the node

    <p align="center">
    <img src="assets/run_node.png" alt="Run the node" width="200">
    </p>

1. When text appears, read the output.

You just interacted with a Large Language Model (LLM). If you kept the default settings, you specifically used OpenAI ChatGPT (GPT-4.1).

While this experience might seem similar to using OpenAI ChatGPT or Anthropic Claude on the web, the real power comes from using LLMs alongside other components in Griptape Nodes. Take another look at the library panel on the left to see all the other nodes available. We're just getting started—there's so much more to explore!

## Summary

In this tutorial, we covered how to:

- Launch Griptape Nodes
- Navigate through the landing page to a workflow
- Get familiar with the Griptape Nodes Editor
- Add your first nodes to the workspace
- Learn about what can connect to what
- Run an agent

## Next Up

In the next section: [Lesson 2: Prompt an Image](../01_prompt_an_image/index.md), we'll start in on the good stuff: making images!
