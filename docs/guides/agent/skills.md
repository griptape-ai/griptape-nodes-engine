# Skills

Skills are markdown files that give the agent extra instructions or domain knowledge. The agent sees each skill's name and description, and reads the full skill when your request calls for it. Skills are read again before every message, so changes take effect without restarting the engine.

## Manage skills in Settings

Open **Settings → Agent Settings** and scroll to the **Skills** section. It lists every skill the agent can find, with:

- **Skill**: the skill's name. A warning icon means the agent can't load it; hover the icon to see why.
- **Description**: when the agent should use the skill.
- **Folder**: which skills folder it came from. Skills from a shared folder are marked **Shared**.
- **Enabled**: turn a skill off to hide it from the agent without deleting it.

From the same section you can:

- Click **New Skill** to write a skill in the editor.
- Click **Import** to add a `SKILL.md` file from your computer.
- Click **Open Folder** to open your skills folder in your file browser.
- Click the trash icon on one of your own skills to delete it. Shared skills can't be deleted from the editor.

## Where skills live

Each skill is a folder with a `SKILL.md` inside. By default, skills live in `.agents/skills/` inside your workspace directory:

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

## Share skills with a team

The `agent.skills_directories` setting lists every folder the agent reads skills from. The first folder is where **New Skill** and **Import** put skills. Add a shared folder, such as one on a network drive, to give everyone the same skills:

```json
{
  "agent": {
    "skills_directories": [".agents/skills", "/mnt/studio/skills"]
  }
}
```

A relative path is resolved against the workspace directory. To set the folders for every user from the environment, use a JSON list:

```bash
GTN_CONFIG_AGENT__SKILLS_DIRECTORIES='[".agents/skills", "/mnt/studio/skills"]'
```

Skills in any folder after the first are marked **Shared** in Settings. Users can turn them off for themselves, but can't delete them from the editor.

If the same skill name appears in more than one folder, the agent uses the one from the earlier folder, so a skill in your own folder replaces a shared one with the same name. Settings shows the other copy with a warning saying which one is used.

## Skill file format

Each `SKILL.md` is a markdown file that starts with a block of settings between two `---` lines:

```markdown
---
name: my-skill-name
description: What this skill does and when the agent should use it.
---

# Skill title

Instructions, reference material, or domain knowledge the agent should apply
when this skill is relevant. Write in plain English. The agent reads this as
part of its context.
```

The `name` must match the skill's folder name and use only lowercase letters, numbers, and single hyphens. The `description` tells the agent when to use the skill. The body can be as long or short as you need: code snippets, step-by-step instructions, reference tables, and so on.

If a `SKILL.md` has a mistake in its frontmatter, the agent leaves that skill out and Settings says what's wrong. Your other skills still load.

Add `disable-model-invocation: true` to the frontmatter to keep a skill in the folder without offering it to the agent. Settings shows it as off, and it stays off until the line is removed.

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

## Where skills apply

Skills apply to the chat sidebar agent. The Agent node in workflows doesn't use them yet.

## Related

- [Agent Skills documentation](https://agentskills.io/home): full reference for the skills format and capabilities
