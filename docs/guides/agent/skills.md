# Skills

Skills are markdown files that give the agent extra instructions or domain knowledge — loaded automatically at startup and reloaded on every run, so edits take effect without restarting the engine.

Griptape Nodes ships with a built-in skill for building and running workflows. You can add your own alongside it.

## Where to put skills

Create a `.agents/skills/` folder inside your workspace directory and give each skill its own folder with a `SKILL.md` inside:

```
<workspace_directory>/
└── .agents/
    └── skills/
        ├── my-skill/
        │   └── SKILL.md
        └── another-skill/
            └── SKILL.md
```

The default workspace directory is `GriptapeNodes/` inside wherever you launched the engine. You can find the exact path in **Settings → File System → Workspace Directory**.

## Skill file format

Each `SKILL.md` is a markdown file with a YAML frontmatter block:

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

The `name` must match the skill's folder name, and `description` tells the agent when to use the skill. The body can be as long or short as you need — code snippets, step-by-step instructions, reference tables, etc.

If a `SKILL.md` has a mistake in its frontmatter, the agent leaves that skill out and logs a warning naming the file. Your other skills still load.

Add `disable-model-invocation: true` to the frontmatter to keep a skill in the folder without offering it to the agent. The agent can still read the skill's files while it uses your other skills, so move a skill out of the folder to keep it private.

## Bundled files

A skill folder can hold more than `SKILL.md`, such as reference documents, templates, or data files. Mention them in the skill's instructions:

```
my-skill/
├── SKILL.md
└── references/
    └── color-palette.md
```

When the agent uses the skill, it learns where the skill's folder is and can look through and read the files in it, including scripts, images, and PDFs. Refer to files by their path inside the skill's folder, such as `references/color-palette.md`. The agent can only read files. It can't change them.

A file can be a symlink as long as it points somewhere inside the skill's folder. The agent can't see links that point outside it, so a skill can't read other files on your computer. The skill folder itself can be a symlink.

## What isn't supported

Griptape Nodes follows the [Agent Skills specification](https://agentskills.io/specification) with these exceptions:

- The agent can read a skill's scripts but can't run them. A skill whose instructions say "run `scripts/extract.py`" won't work.
- The agent can read images and PDFs, but not other binary files.
- Frontmatter fields that change how a skill runs, such as `allowed-tools` and `model`, are ignored. The engine logs a warning naming the skill and the fields.
- A `description` longer than 1,024 characters still loads, with a warning in the log.
- `license`, `compatibility`, and `metadata` are accepted but not checked or used.

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

    Skills are picked up automatically — no engine restart needed. Add a new skill folder under `.agents/skills/`, and the very next message you send will use it.

## Related

- [Agent Skills documentation](https://agentskills.io/home) — full reference for the skills format and capabilities
