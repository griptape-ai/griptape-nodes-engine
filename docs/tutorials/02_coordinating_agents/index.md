# Lesson 3: Coordinating Agents

Welcome to the third tutorial in our Griptape Nodes series! In this guide, you'll learn how to coordinate multiple agents within a workflow to perform sequential tasks—specifically, translating stories between languages and summarizing them.

## What we'll cover

In this tutorial, we will:

- Study then recreate a translation workflow between agents working sequentially
- Discover how to "merge texts" to take outputs and modify them into new prompts
- Learn about the "exec chain" for controlling workflow execution order
- Build more into the template workflow to add a summarization task

By the end of this journey, you'll understand how to create workflows where agents build upon each other's work, passing information seamlessly between specialized tasks. This foundation will prepare you for creating sophisticated AI systems that can handle multi-step processes requiring different types of intelligence at each stage.

Let's begin by examining a simple translation workflow that demonstrates these principles in action.

## Navigate to the Landing Page

To begin this tutorial, go to the landing page via the nav bar with the Griptape Nodes logo in the top left. Locate and open the example workflow called "coordinating_agents" at the top of the page.

<p align="center">
  <img src="assets/coordinating_agents_example.png" alt="Coordinating Agents example">
</p>

## Explore the Template Workflow

When the template loads, you'll see a workflow with the following components:

<p align="center">
  <img src="assets/workflow_overview.png" alt="Workflow overview">
</p>

1. **Agent Node (spanish_story)**: Generates a four-line story in Spanish
1. **Merge Text Node**: Combines the Spanish story with "Rewrite this in English"
1. **Second Agent Node (to_english)**: Translates the merged prompt into English
1. **Display Text Node**: Shows the final English translation

This workflow demonstrates how multiple agents can each perform their own distinct "jobs."

By connecting one agent's output to another through a **MergeTexts** node, you create _new_ prompts that direct the next agent's behavior.

## How we're using the MergeText node here

All a **MergeTexts** node does is combine incoming texts using the "merge string" as a separator. The default merge string is two newlines: `\n\n`. In this example, I've typed "Rewrite this in English:" into **input_1** of the MergeTexts node and connected the output of my **spanish_story** node to **input_2**. When run, the **MergeTexts** node will output:

> Rewrite this in English:
>
> Bajo la luna, el río cantó,
> Un secreto antiguo en su agua dejó.
> La niña lo escuchó y empezó a soñar,
> Que el mundo era suyo, listo para amar.

You can see how this method of creating new prompts out of the results of other nodes can allow for a sophisticated multi-agent workflow where the first agent writes a Spanish story, and the second agent translates it to English. Your final output will be the English translation of whatever unique Spanish story was generated.

<p align="center">
    <img src="assets/workflow_result.png" alt="Workflow result"  width="500">
  </p>

!!! info

    You should expect variability in these from run-to-run. That's okay! Remember, talking with an agent can in a way be like talking to a person. You may get slightly different answers if you ask them the same question many times.

## Build a sibling workflow

This is what we're aiming to get to:

<p align="center">
  <img src="assets/sibling_target.png" alt="Sibling target" width="500">
</p>

Now it's time to build your own workflow. Create another nearly identical flow just below this one to practice creating and connecting nodes. Add the following to your workflow:

1. Two **Agents** ( agents > Agent )
    \- These are agents that interact with LLMs (Like ChatGPT, or Claude)
1. A **MergeTexts** node ( text > MergeTexts )
    \- A node to accept multiple texts and output them "merged"
1. A **DisplayText** ( text > DisplayInput )
    \- A node to simply display text output for easier viewing

## Configure the First Agent

Set up your first agent to generate content in your chosen language:

1. In the first agent node, enter: `Write me a four line story in [your chosen language]` (e.g., Mandarin, French, etc.)
1. This agent will generate the initial story that we'll translate

<p align="center">
  <img src="assets/mandarin.png" alt="Story setup" width="300">
</p>

## Connect to the MergeTexts Node

Next, prepare the translation prompt:

1. Type directly into the MergeTexts node's **input_1** field and enter: `Rewrite this in English`
1. Connect the output from the first Agent to **input_2** of the MergeTexts node

<p align="center">
  <img src="assets/mandarin_merge.png" alt="Merge text setup">
</p>

## Configure the Second Agent

Set up the translator agent:

Connect the output of the MergeTexts node to the second Agent node's **prompt**. This agent will now receive both the original story, and the instruction to translate it

<p align="center">
  <img src="assets/mandarin_to_english.png" alt="Second agent setup">
</p>

## Display the Result

To see the final translation:

1. Connect the output of the second Agent to the DisplayText node
1. Run your workflow.
1. After the workflow runs, this node will show the translated English text:

<p align="center">
  <img src="assets/mandarin_display.png" alt="Display setup"  width="500">
</p>

## Understanding Execution Order (Exec Chain)

A key concept in Griptape Nodes is the execution chain. As workflows become more complex, controlling the order of execution becomes important. Let's explore this concept.

1. Notice the "exec in" and "exec out" pins (half-circle connectors) on nodes
1. These define the order in which nodes run
1. For complex workflows, connect the exec ports in the order you want execution to occur
1. This ensures nodes run in the intended sequence, even with complex data flows

<p align="center">
  <img src="assets/exec_chain.png" alt="Execution chain">
</p>

!!! info

    Griptape Nodes will automatically determine the execution order of nodes by analyzing their dependencies.

    However, when you need more precise control over the execution sequence, you can use the exec chain feature. This provides a way to explicitly define the order you want when the automatic dependency detection might not align with your intended behavior.

    There is no cost or penalty to using the exec chain anytime you want, except for the possibility of forcing things to execute in a faulty order. For most simple flows, it is unnecessary.

## Expand the Workflow: Summarize Multiple Stories

Let's enhance our workflow to handle summarization of _all_ the stories:

1. Add another new **MergeTexts** node that combines both English translations
    1. In this merge text node, enter: `Summarize both these stories` in **input_1**
    1. Connect both the translation nodes' **outputs** into **input_1** and **input_2** on the MergeTexts node
1. Add another **Agent** node
1. Connect the MergeTexts **output** into the **prompt** for your new agent
1. Connect the agent **output** to a new **DisplayText** node
1. Optionally, use exec chain connections to ensure this summary step runs last (you can even connect _everything_ up to run in the order you want)

<p align="center">
  <img src="assets/summary_pre.png" alt="Expanded workflow">
</p>

## Run the Complete Workflow

Execute your expanded workflow and observe the process:

1. The first agents generate stories in different languages
1. The merge text nodes create prompts to translate them
1. The second agents translate the stories into English
1. The summary agent combines and summarizes both translations
1. The display nodes show all the results

<p align="center">
  <img src="assets/final_result.png" alt="Final result" width="500">
</p>

!!! info

    Again, remember! Look for this _construction_ in the response you get, not that it matches what you see here - it is likely to be wildly different!

## Summary

In this tutorial, we covered:

- How a workflow can hand things off between agents to perform tasks like translation
- Discovered how "merge texts" allows you to take outputs and modify them into new prompts
- Learned about the "exec chain" for controlling workflow execution order
- Built more into the template workflow to add a summarization task

## Next Up

In the next section: [Lesson 4: Compare Prompts](../03_compare_prompts/index.md), we'll learn how to get AIs to bucket-brigade, where agents pass work sequentially, through flows!
