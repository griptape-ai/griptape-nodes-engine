# EndFlow

## What is it?

The End Flow node marks the end of your workflow and defines its **outputs**. Every parameter you add to it becomes an output of the workflow: a value handed back to the caller when the workflow runs from the terminal, as a node in another workflow, or after it's published. It also records whether the run succeeded.

## When would I use it?

Use this node when you want to:

- Mark where your workflow ends
- Return values from your workflow
- Report whether the workflow succeeded or failed
- Make the workflow callable: running it from the terminal with inputs, using it as a node, or publishing it all need a [Start Flow](start_flow.md) and an End Flow

## How to use it

### Basic Setup

1. Add the End Flow node to your workflow
1. Connect the last node in your flow to **Succeeded**, or to **Failed** for a branch that handles an error
1. Add a parameter for each value the workflow should return (**Add Parameter** in the node's menu or the Properties panel), and connect the values to it
1. Save the workflow. The outputs are recorded in the workflow file when you save

### Parameters

- **exec_in** (Succeeded) - control input for a run that succeeded
- **failed** (Failed) - control input for a run that failed
- **result_details** (in the collapsed **Status** group) - a message describing the result
- **Your own parameters** - each one becomes a workflow output with the same name

### Outputs

- **Your own parameters** - returned to the caller as the workflow's outputs

**was_successful** (in the **Status** group) is a read-only property, not a pin: `true` if the flow arrived through **Succeeded**, `false` if through **Failed**. It's returned to a caller alongside your own outputs when the workflow runs from the terminal.

## Example

Imagine a workflow that writes text about a topic you choose:

1. Create a [Start Flow](start_flow.md) node with a text parameter named `topic`
1. Connect it to an Agent that writes about the topic
1. Add an End Flow node with a text parameter named `text`, and connect the Agent's output to it
1. Connect the Agent's control output to **Succeeded**

The workflow now returns `text` to whatever runs it.

## Important Notes

- When it runs, End Flow sets `was_successful`, prefixes `result_details` with `[SUCCEEDED]` or `[FAILED]`, and publishes its parameter values as the workflow's outputs
- A workflow can have more than one End Flow, for example one per branch; all their parameters become outputs
- When a library ships the workflow as a node, the **Status** parameters aren't exposed on it; see [Nodes From Workflow Files](../../development/custom_nodes/authoring_libraries.md#nodes-from-workflow-files)

## Common Issues

- **Flow Continues Past End Flow**: Make sure nothing is connected after the End Flow node
- **Flow Doesn't Reach End Flow**: Check your connections so the flow can reach it
- **An output is missing**: Save the workflow after adding the parameter; outputs are read from the saved file
