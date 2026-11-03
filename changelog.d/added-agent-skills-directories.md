- The `agent.skills_directories` setting lists the folders the chat agent loads skills from, so a
  team can share one folder of skills. When two folders hold a skill with the same name, the earlier
  folder's is used. It defaults to `.agents/skills` in the workspace, and
  `GTN_CONFIG_AGENT__SKILLS_DIRECTORIES` takes a JSON list.
  [#6097](https://github.com/griptape-ai/griptape-nodes-engine/issues/6097)
