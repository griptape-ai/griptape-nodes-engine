# Skills

Skills are markdown files that give the agent extra instructions or domain knowledge — loaded automatically at startup and reloaded on every run, so edits take effect without restarting the engine.

Griptape Nodes ships with a built-in skill for building and running workflows. You can add your own alongside it.

## Where to put skills

Create a `.agents/skills/` folder inside your workspace directory and drop skill files in there:

```
<workspace_directory>/
└── .agents/
    └── skills/
        ├── my-skill.md
        └── another-skill.md
```

The default workspace directory is `GriptapeNodes/` inside wherever you launched the engine. You can find the exact path in **Settings → File System → Workspace Directory**.

## Skill file format

Each skill is a markdown file with a YAML frontmatter block:

```markdown
---
name: my-skill-name
description: What this skill does and when the agent should use it.
---

# Skill title

Instructions, reference material, or domain knowledge the agent should apply
when this skill is relevant. Write in plain English — the agent reads this as
part of its context.
```

The `name` and `description` fields tell the agent what the skill is for and when to use it. The body can be as long or short as you need — code snippets, step-by-step instructions, reference tables, etc.

## Example: a house style guide

```markdown
---
name: writing-style
description: Apply our house style when drafting or editing text.
---

# Writing Style Guide

- Use sentence case for headings, not title case.
- Prefer active voice.
- Avoid jargon; explain technical terms on first use.
- Maximum sentence length: 25 words.
```

## Hot reload

!!! tip

    Skills are picked up automatically — no engine restart needed. Drop a new `.md` file into `.agents/skills/`, and the very next message you send will use it.

## Related

- [Agent Skills documentation](https://agentskills.io/home) — full reference for the skills format and capabilities
