# Writing Docs for Your Library

A library ships its own docs. Put markdown files in a `docs/` folder next to the library's JSON file, and they show up in the editor's **Docs** panel and are served to agents. You don't host anything, the docs always match the version of the library that is installed, and they work with no network.

You can start with no docs at all. Every node gets a page built from its description and its parameters, and every MCP tool gets one built from its description and inputs. Write pages where the built one isn't enough.

## What goes where

Four folders under `docs/` hold one page for each thing the library registers. Everything else under `docs/` is about the library as a whole.

| Folder                 | One page for each             | Name the page after                                                                            |
| ---------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------- |
| `docs/nodes/`          | node in `nodes`               | the node's class name (`LoadAudio.md`), or its display name with underscores (`load_audio.md`) |
| `docs/workflow_nodes/` | node in `workflow_nodes`      | the node type                                                                                  |
| `docs/workflows/`      | template in `workflows`       | the template's file name without `.py`                                                         |
| `docs/tools/`          | MCP tool from `get_mcp_tools` | the tool's request type, such as `SearchDocsRequest.md`                                        |

```text
my-library/
  griptape_nodes_library.json
  docs/
    index.md                  # the library's overview page
    guides/
      getting-started.md      # a page about the library as a whole
    nodes/
      LoadAudio.md            # documents the node LoadAudio
    workflows/
      color_managed_load.md   # documents workflows/templates/color_managed_load.py
    tools/
      SearchDocsRequest.md    # documents the MCP tool SearchDocsRequest
    .agents/skills/           # skills for agents that use the library (see below)
```

A few rules apply to every page:

- The filename is the only link between a page and what it documents. The manifest has no field for it, so a rename can't leave the two out of step.
- Pages can sit in subfolders, such as `docs/nodes/audio/LoadAudio.md`. The folders are your choice.
- Spell a class name exactly. `loadaudio.md` works on a case-insensitive disk and fails on others.
- If two pages claim the same node, the one with the fewest folders wins, then the first by path.
- A page that matches nothing is never shown. If a page doesn't appear, check its name.
- A page named after a display name stops matching when the display name changes. A page named after the class survives it.

Everything under `docs/` outside those four folders is a library page. Folders and pages are listed as they sit on disk, alphabetically, with `index.md` first. To choose the order, put a number in front of the filename (`01-getting-started.md`). The number isn't shown. A folder whose name starts with a dot is never shown as pages.

Without an `index.md`, the editor shows the library's README as its overview.

## Frontmatter

Every page can start with a few optional fields.

```yaml
---
title: Shown instead of the default title
description: One or two sentences on what the page covers.
when_to_use: When to pick this over something else.
---
```

- `title` replaces the default. A node page without one uses the node's display name, a tool page uses the request type, and a library page uses its first heading.
- `description` is the line under the title. On a node or tool page it replaces the description the library registered.
- `when_to_use` is guidance for agents choosing which page to open.

Unknown fields are ignored.

## What a good node page has

Keep a node page short. A scaffold of two sections is enough:

```markdown
## When to use it

You need a whole-number age and don't want someone typing 400.

## Example

In [Getting started](../guides/getting-started.md), the age feeds the introduction node.
```

Don't list the parameters by hand. The panel shows a table of them under your page. When a tooltip isn't enough, add a `## Parameters` section with one `###` heading per parameter, and the panel shows each explanation beside that parameter in the table.

```markdown
## Parameters

### age

The slider runs from 1 to 98. A value typed in is held inside that range.
```

### Write tooltips as the minimum docs

A node with no page is documented by its parameter tooltips, so they matter most. Write them in sentence case and say what the value does. "How many steps to run. More steps look better and take longer." tells an artist something. "Steps" doesn't.

## Pages for MCP tools

A library can expose request types to agents with `get_mcp_tools` (see [Advanced Libraries](advanced_libraries.md#get_mcp_tools)). The editor offers your library as an MCP server, and the Docs panel lists its tools under **MCP server**, whether or not the library has nodes.

Without a page, a tool is documented by:

- **Its docstring.** The request class's docstring becomes the description. Write it for an agent choosing between tools: what the tool does, and when to use it.
- **Its fields.** The table of inputs comes from the request's fields. A field's description is the part of the table that explains it, so give each field a description.
- **Its read-only flag.** `MCPTool(request, read_only=True)` shows "only reads". Without it the panel says the tool can change things.

To say more, add `docs/tools/<RequestType>.md`. Its `title` and `description` replace the generated ones, and the text shows above the inputs table. Spend the words on what a docstring can't carry: a worked example, the order to call tools in, and what comes back.

## Pages for workflow templates

A template without a page still shows a thumbnail and a description. Both come from the metadata header of the template file, so set `description` and `image` there. `image` is a path next to the template file or a web address. A path that doesn't exist is ignored. A page in `docs/workflows/` adds the author's text under them, and its `description` wins over the file's.

## Links, images and formatting

Pages are plain markdown that reads well on GitHub and in the editor.

- Tables, fenced code blocks and `> [!NOTE]` and `> [!WARNING]` callouts work. Raw HTML is removed.
- Link to another page with a relative path, such as `../guides/color-management.md#aces`. The editor opens it in the panel. Linking to a node's page opens the node, with its parameters.
- `node:LoadImage` links to a node in the same library and survives moving files around. GitHub doesn't render it, so use a relative path where GitHub readers matter.
- Put images in the library and use a relative path, such as `![The node](assets/load-audio.png)`. A web image won't load offline.
- Keep to one idea per page and one heading level per idea. Short sentences read better in a narrow panel.

## Skills for agents

An agent that builds workflows with your library reads skills in `docs/.agents/skills/`. Each skill is a folder with a `SKILL.md`.

```text
docs/.agents/skills/color-managed-workflow/SKILL.md
```

```markdown
---
name: color-managed-workflow
description: Build a workflow that keeps colors correct from load to save.
---

1. Start with Load Image set to its original colorspace.
2. ...
```

Skills for people or agents working on the library's code don't go here. Keep those in `.agents/skills/` at the top of the repo, and developer docs in `dev/docs/`. The engine never reads either.

## Keeping the manifest and the docs apart

The manifest has one docs field, and it's optional.

```json
{
  "documentation_path": "../docs"
}
```

It defaults to `docs`. Set it when the manifest sits in a package subfolder and `docs/` sits at the repo root. It's a path relative to the manifest, like the path of a widget.

## Try it

1. Add a page named after a node and reload the library.
1. Open the **Docs** panel in the editor's right sidebar and pick your library.
1. Your page appears under the node, with the parameter table below it.

A page you add shows up after the library is loaded again. A change to the text of a page you already have shows up the next time you open it.

The [library template](https://github.com/griptape-ai/griptape-nodes-library-template) has a working example of every folder above, including a tool page.
