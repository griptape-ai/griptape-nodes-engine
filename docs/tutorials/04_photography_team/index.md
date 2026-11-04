# Lesson 5: Build a Photography Team

Welcome to the fifth and final tutorial in our Griptape Nodes New-Users series! In this guide, we'll build on our prompt engineering concepts and introduce additional nodes that provide greater precision and control. We'll examine a sophisticated system of coordinated agents that collaborate like a team to generate spectacular image prompts, while also learning several important new nodes along the way.

## What we'll cover

In this tutorial, we will:

- Learn about what rule sets are and what they do
- Learn about tools
- Learn about "list" parameters and interacting with them
- See how we can convert agents _into_ tools
- Coordinate multiple agents-as-tools through a single orchestrator
- Generate high-quality image prompts through team collaboration

## Navigate to the Landing Page

To begin this tutorial, return to the main landing page by clicking on the Griptape Nodes logo at the top left of the interface.

## Open the Photography Team Example

On the landing page, locate and click on the **"Build a Photography Team"** tile to open this example workflow.

<p align="center">
  <img src="assets/photography_team_example.png" alt="Photography Team example">
</p>

## Overview of the Workflow

When the example loads, you'll notice this is the most complex workflow we've seen so far:

<p align="center">
  <img src="assets/workflow_overview.png" alt="Workflow overview">
</p>

The workflow consists of several key components:

- Multiple specialized Agents
    - Cinematographer
    - Color Theorist
    - Detail Enthusiast
    - Image Generation Specialist
- Rulesets for each RuleSetList
- RuleSetLists for each Agent
- AgentToTool converters
- A ToolList
- An orchestrator Agent
- A GenerateImage node

You'll notice that the pattern upstream of the TooList repeats the same structure four times. Each upstream segment follows identical logic and structure, differing only in the specific data being processed. Let's examine just one instance in detail, as understanding this single example will give you the conceptual framework for all four occurrences.

**RuleSet->RuleSetList->Agent->AgentToTool**

<p align="center">
  <img src="assets/rule_ruleset_agent_tool_chain.png" alt="RuleSet->RuleSetList->Agent->AgentToTool">
</p>

## RuleSets

RuleSets are nodes that define how agents should approach their tasks.

Within each RuleSet, you can have multiple rules. Rules are understood to be separated by appearing on their own line using intentional line breaks. Don't worry about word wrapping - if text simply flows to the next line, it's still considered part of the same rule.

Every RuleSet requires a unique name, which agents use to identify and distinguish between different RuleSets. When working with multiple RuleSets, choosing clear, distinctive names becomes especially important for proper organization and reference.

!!! tip

    This workflow is constructed so that each agent only has one RuleSet, so the chance for a RuleSet name collision is zero, but it's still good practice to name these well.

<p align="center">
  <img src="assets/cinematographer_ruleset.png" alt="Cinematographer rule set" width="800">
</p>

The Cinematographer RuleSet you see above contains detailed instructions covering framing, composition, and visual storytelling techniques. These rules influence how the agent tends to respond—establishing preferred patterns for its outputs.
For more predictable performance, you guide the agent through these RuleSets. It's helpful to ensure your rules maintain reasonable consistency with one another. Contradictory or conflicting instructions may lead to mixed results, as the agent attempts to balance competing priorities in its responses.

## RuleSetLists

RuleSetLists function as collectors or containers, similar to another node we'll talk about momentarily, ToolLists. RuleSetLists serve a specific organizational purpose: allowing you to gather multiple Rulesets into a single collection that can connect to "list" parameters—parameters designed to accept multiple items of the same type.

I've included RuleSetLists here primarily to introduce you to them rather than for any immediate functional value.

<p align="center">
  <img src="assets/ruleset_list.png" alt="RuleSetList" width="300">
</p>

!!! info "Optional Playtime"

    If you'd like to independently explore changing the graph harmlessly to work around the RuleSetList:

    1. On the Agent, uncollapse the **Advanced Options**

    1. Disconnect the RuleSetList from the Agent's **ruelsets** parameter

    1. The agent's display will change slightly, and you should see **add item to rulesets** underneath the
        **rulesets** parameter. Click that, and you should see a new port will appear. This new port takes in a _single_ RuleSet.

    1. Connect directly from the RuleSet's **ruleset** parameter to the new port.

    <p align="center">
      <img src="assets/ruleset_direct_connect.png" alt="RuleSet direct-connect" width="500">
    </p>

## Understanding Tools in Griptape Nodes

**Tools** extend an agent's capabilities by connecting it to external functions and services. When faced with tasks beyond its internal knowledge—like retrieving current data or performing calculations—tools provide the bridge. The agent decides when to use a tool, formats the appropriate request, and interprets the returned results. This transforms agents from isolated text generators into assistants that can access databases, run code, or manipulate files while maintaining their reasoning abilities throughout a conversation.

Tools consist of two key components:

1. **Underlying code**: This flexible, open-ended component can be anything from simple calculations to complex integrations.

1. **Description**: This precise element communicates the tool's purpose and appropriate use cases to the agent, so it knows when and how to use the tool.

As of initial release, we have just a few important tool nodes like a Calculator and DateTime, but we've also included what can easily become the most adaptive tool of them all: Agents _as_ Tools.

<p align="center">
 <img src="assets/tools_concept.png" alt="Tools concept">
</p>

## Converting Agents to Tools

Griptape Nodes allows you to turn entire agents into tools. This allows a primary agent to delegate specific tasks to specialized agents, creating a hierarchical system where expertise is distributed. When agents can "stay in their lane" and focus on specific domains, they perform more reliably—just like people who can concentrate on one task rather than juggling many simultaneously.

This crazy-powerful concept is part of Griptape Nodes via the **AgentToTool** converter node.

<p align="center">
 <img src="assets/agent_tool_conversion.png" alt="Agent to tool conversion" width="500">
</p>

While fully-formed tools typically handle both components (underlying code, description) under-the-hood, agents-as-tools require you to provide the description part yourself. This is where you establish the boundaries and expectations for how that agent should function as a tool. The description you write helps the orchestrator agent understand when and how to use your specialized agent-as-tool.

## The ToolList node

The ToolList functions similarly to the RuleSetList discussed earlier, but for tools instead of RuleSets. It serves as a collector that gathers multiple tools into a single collection, which can then connect to parameters accepting tool lists.
Just as the RuleSetList organizes RuleSets, the ToolList provides a streamlined way to pass multiple tools to an agent. This becomes especially valuable when your agent needs access to several specialized tools.
This organizational pattern is consistent throughout Griptape Nodes - "list" nodes act as waypoints, collecting multiple items of the same type before sending them to components that work with collections.

!!! info "Optional Playtime"

    Just as you might have bypassed the RuleSetList earlier for practice, you can do the same with the ToolList. Simply disconnect it from the orchestrator agent, click "add item to tools" to create a new port, and connect your tool directly. This change won't affect how your workflow functions.

<p align="center">
  <img src="assets/tool_list.png" alt="Tool list">
</p>

## The Orchestrator

The central component of this workflow is the orchestrator agent:

1. Locate the orchestrator agent

1. Notice it has its own rule set:

    ```
    You are creating a prompt for an image generation engine.
    You have access to topic experts in their respective fields.
    Work with the experts to get the results you need.
    You facilitate communication between them.
    If they ask for feedback, you can provide it.
    Ask the image generation specialist for the final prompt.
    Output only the image generation prompt. Do not wrap it in markdown context.
    ```

1. The orchestrator's prompt is actually very simple:

    "Use all the tools at your disposal to create a spectacular image generation prompt about a skateboarding lion."

    <p align="center">
    <img src="assets/orchestrator_setup.png" alt="Orchestrator setup">
    </p>

## How the Workflow Functions

The entire system operates through this process:

1. The orchestrator receives a relatively simple prompt
1. It has access to all specialized tools (converted agents)
1. The orchestrator can call upon:
    - The Cinematographer for framing and composition guidance
    - The Color Theorist for color palette recommendations
    - The Detail Enthusiast for intricate details to include
    - The Image Generation Specialist for formatting the final prompt
1. The final output connects to the GenerateImage node

## Running the Workflow

Let's execute the workflow to see the photography team in action:

1. Run the workflow
1. Observe as the orchestrator calls upon different specialized tools (it is a quick flash, but you can see them all "tagged in")
1. The final output is collected into a sophisticated image prompt
1. This prompt is then used to generate the image

<p align="center">
  <img src="assets/workflow_result.png" alt="Workflow result" width="500">
</p>

## Summary

In this tutorial, we covered:

- Learning about what rule sets are and what they do
- Learning about tools
- Seeing how we can convert agents _into_ tools
- Seeing what a "team" of specialized AI experts looks like
- Coordinating multiple agents through an orchestrator
- Generating high-quality image prompts through team collaboration

These advanced techniques showcase the full power of Griptape Nodes for creating complex, collaborative AI systems.

Thank you for completing this series of tutorials. We're excited to see what you'll build with these powerful tools!

If you're more in the mood to keep going to something more advanced, please continue on to our "I'm A Pro" series (\*Coming Soon!)
