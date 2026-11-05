- The chat agent's skills can be managed from **Settings → Agent Settings → Skills**: see each skill
  and why one can't load, turn skills on or off, write or import a skill, and open the skills folder.
  The engine answers `ListAgentSkillsRequest`, `SetAgentSkillEnabledRequest`,
  `CreateAgentSkillRequest`, `ImportAgentSkillRequest`, and `DeleteAgentSkillRequest`. Turned-off
  skills are kept in `agent.disabled_skills`, and `GTN_CONFIG_AGENT__DISABLED_SKILLS` takes a JSON
  list.
  [#6097](https://github.com/griptape-ai/griptape-nodes-engine/issues/6097)
