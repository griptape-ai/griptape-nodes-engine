# StartFlow

## What is it?

The Start Flow node marks the beginning of your workflow and defines its **inputs**. Every parameter you add to it becomes an input of the workflow: a value the caller supplies when the workflow runs from the terminal, as a node in another workflow, or after it's published.

## When would I use it?

Use this node when you want to:

- Mark where your workflow starts
- Give your workflow inputs that a caller fills in
- Make the workflow callable: running it from the terminal with inputs, using it as a node, or publishing it all need a Start Flow and an [End Flow](end_flow.md)

## How to use it

### Basic Setup

1. Add the Start Flow node to your workflow
1. Connect **Flow Out** to the first node in your flow
1. Add a parameter for each value the workflow should take as input (**Add Parameter** in the node's menu or the Properties panel), and connect each one to the nodes that use it
1. Save the workflow. The inputs are recorded in the workflow file when you save

### Parameters

- **Your own parameters** - each one becomes a workflow input with the same name. The value you set on the parameter in the editor becomes the input's default

### Outputs

- **exec_out** (Flow Out) - passes control to the next node in the flow
- **Your own parameters** - pass the input values to the nodes they're connected to

## Example

Imagine a workflow that writes text about a topic you choose:

1. Create a Start Flow node and add a text parameter named `topic`
1. Connect **Flow Out** and `topic` to an Agent that writes about the topic
1. Connect the Agent's output to an [End Flow](end_flow.md) parameter named `text`
1. Save the workflow

Running it from the terminal now takes `--topic` and returns `text`. **Publish Workflow → Run Workflow from the terminal** in the editor header shows the exact command.

## Important Notes

- A workflow can run in the editor without a Start Flow. You need one, plus an [End Flow](end_flow.md), for the workflow to have inputs and outputs
- A workflow can have more than one Start Flow; all their parameters become inputs
- Control parameters such as **Flow Out** aren't inputs of a workflow used as a node. The terminal command still lists them (`--exec_out`); leave them unset
- Library authors can ship a workflow as a node whose inputs are its Start Flow parameters; see [Nodes From Workflow Files](../../development/custom_nodes/authoring_libraries.md#nodes-from-workflow-files)

## Common Issues

- **Flow Doesn't Run**: Make sure **Flow Out** is connected to the next node
- **An input is missing**: Save the workflow after adding the parameter; inputs are read from the saved file
