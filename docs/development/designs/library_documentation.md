# Design: user-authored node documentation in libraries

Tracking issue: [griptape-ai/internal#222](https://github.com/griptape-ai/internal/issues/222).

> This page is for the team to comment on and revise. It's not part of the published docs, so it stays out of `mkdocs.yml`. Please don't add it to the nav or the `llmstxt` sections.

## Goals

- Library authors write docs in their own repo and ship them with the library. They don't host anything and they don't touch the engine.
- Users read those docs in the editor, or on GitHub where the library lives. In the editor they come from the installed library, so they always match the version you're running, and they work with no network, air-gapped included.
- Agents read the same docs through the engine.
- A node with no docs still gets a page. The engine builds a basic one from what it already knows about the node.
- A library gets an overview page and a navigation tree, not just a pile of node pages. The overview is the library's `docs/index.md`, or its README if it doesn't have one. The tree follows the folders under `docs/`, with the library's nodes listed by category below it.

## Proposal

### Location and file layout

Docs live in a `docs/` folder next to `griptape_nodes_library.json`.

Three folders under `docs/` are special, one for each list in the manifest: `docs/nodes/` holds one page per node, `docs/workflow_nodes/` one page per workflow node, and `docs/workflows/` one page per workflow template. Everything else under `docs/` is about the library as a whole, like a getting-started guide or a file format reference, and the library can name and arrange those however it wants. The `guides/` and `reference/` folders below are just examples. A library can have none of them.

How each one maps to `griptape_nodes_library.json`, plus the skills folders and the folder that sits outside `docs/`:

| Manifest list    | Folder                 | What it holds                                                                      |
| ---------------- | ---------------------- | ---------------------------------------------------------------------------------- |
| `nodes`          | `docs/nodes/`          | one page per node, named after its `class_name`                                    |
| `workflow_nodes` | `docs/workflow_nodes/` | one page per workflow node, named after its `node_type`                            |
| `workflows`      | `docs/workflows/`      | one page per template, named after its filename without `.py`                      |
| n/a              | `docs/.agents/skills/` | skills for agents that use the library, one folder per skill                       |
| n/a              | `.agents/skills/`      | skills for coding agents working on the library, never loaded by the sidebar agent |
| n/a              | `dev/docs/`            | docs for node developers, in any shape. The engine never reads it                  |

The library's overview page is `docs/index.md`. If there isn't one, the README stands in.

Docs for people who work on the library, like how to add a model or what a node page should look like, don't go in `docs/`. Everything under `docs/` shows up in the editor, so put contributor docs somewhere else, such as `dev/docs/`. The engine only reads the docs path, so those pages stay out of the editor.

```
my-library/
  griptape_nodes_library.json
  .agents/                    # for coding agents working on the library. The sidebar agent never loads these
    skills/
      run-the-tests/
        SKILL.md              # same format as other skills
  nodes/                      # Python source. Its folders have nothing to do with the docs folders
    audio/
      load_audio.py           # defines class LoadAudio
      mix_audio.py            # defines class MixAudio
    image/
      load_image.py           # defines class LoadImage
  workflows/
    shout_text.py             # saved workflow, registered as the node type "ShoutText"
    templates/
      color_managed_load.py   # a workflow template, listed under "workflows" in the manifest
  docs/
    index.md                  # library overview. The README stands in if this is missing
    guides/                   # an example folder, name these however you like
      getting-started.md      # about the library, not one node
      color-management.md
    reference/                # another example
      file-formats.md
    nodes/                    # a fixed folder: one page per node
      audio/
        LoadAudio.md          # documents LoadAudio
        LoadAudio.fr.md       # French version (locales come later, the name is reserved now)
        MixAudio.md           # documents MixAudio
      LoadImage.md            # doesn't need to sit in a folder
    workflow_nodes/           # the second fixed folder: one page per workflow node
      ShoutText.md            # documents the workflow node ShoutText
    workflows/                # the third fixed folder: one page per workflow template
      color_managed_load.md   # documents workflows/templates/color_managed_load.py
    .agents/                  # for the agent that uses the library. A dot-folder is never shown as pages
      skills/
        color-managed-workflow/
          SKILL.md            # same format as other skills
    assets/
      screenshot.png
  dev/                        # anything for people working on the library
    docs/                     # docs for node developers. The editor never shows these
      adding-a-model.md
```

Node pages:

- A page matches a node by its filename. Name it after the node's `class_name` (`LoadAudio.md`), or after its display name in snake_case (a node shown as "Pipeline Builder" gets `pipeline_builder.md`, and hyphens work too). Workflow nodes work the same way with `node_type`, and templates use their filename without `.py`.
- The match is on the node, not the Python file. `load_audio.py` documents nothing by itself, and a file that defines several node classes gets one page per class. Spell a class name exactly: `loadaudio.md` works on a case-insensitive filesystem and fails on others, so the docs check flags it.
- Pages can sit in subfolders of `docs/nodes/` however you like. The engine indexes them when the library loads, so a new or moved page shows up after a library refresh. The folders don't have to match your source folders or node categories, which change on their own.
- Locale variants will be `<Key>.<locale>.md`, so `LoadAudio.fr.md` is the French page for `LoadAudio`. The engine splits a filename at its first dot, so that never reads as a node called `LoadAudio.fr`. Locales aren't in v1, and the engine ignores those files for now.
- If two pages claim the same node, the one with the fewest folders wins, then the first path alphabetically. The engine reports a library problem naming both, and the docs check fails. Docs problems never stop a library from loading.
- A node with no page still gets one, built from its description and the parameter table. A page that matches no node is an orphan: the engine reports it when the library loads, and the docs check fails on it. A page named after the class survives a display name change. A page named after the display name doesn't, so it becomes an orphan until someone renames it. Renaming the class itself changes the key, which also breaks saved workflows, so it's rare, and the orphan report points to the file.

Library pages:

- Everything under `docs/` outside the top-level `nodes/`, `workflow_nodes/` and `workflows/` folders is a library page. Those three names are reserved at the top level only, so `docs/guides/nodes/` is an ordinary page folder. A folder whose name starts with a dot is never shown as pages. A library page's title follows the title rule under File format.

- Navigation is the folder hierarchy. There is no navigation file. The panel shows folders and pages as they sit on disk, so adding a page adds an entry. A folder's title is its name with dashes and underscores turned into spaces and the first letter capitalized, so `color-management/` shows as "Color management".

- Entries are sorted alphabetically by filename, case-insensitive, with `index.md` first. An author who wants a different order puts a number prefix on the filenames (`01-getting-started.md`, `02-color-management.md`). A leading number prefix is not shown in a title or a folder name.

- Nodes are not part of the page tree. The editor lists them from the manifest, grouped by category.

Manifest field:

- The manifest gets one optional field, `documentation_path`, default `"docs"`. It is a path relative to the manifest, like `widgets[].path`. This is a minor schema bump. An author who keeps the manifest in a package subfolder and wants `docs/` at the repo root sets `"../docs"`.

Other node sources:

- The rule is the same for every source: the folder that registers a node is its root, and its docs live in `docs/` under that folder. The sandbox directory (Settings, Library, Sandbox Settings) is the root for sandbox nodes, with no manifest, so `<sandbox>/docs/nodes/<Key>.md` documents a sandbox node. A saved workflow in the sandbox becomes a node named after the workflow, and that name is its key.

### Packaging

- A library is installed as a whole repository, so everything in the repo ships with it, including a `docs/` folder at the repo root. An author who keeps the manifest in a package subfolder can leave `docs/` at the repo root and set `documentation_path` to `"../docs"`.
- Bundling video in the repo grows every clone of the library. The authoring guide recommends short looping clips for node demos and a link to hosted video for anything long. The docs check warns about large files (see below).

### File format

The format is a portable subset of markdown. It renders on GitHub, reads cleanly as raw text for agents, and does not depend on any site generator.

- CommonMark plus GFM tables and fenced code blocks with language highlighting.
- Callouts use GitHub alerts (`> [!NOTE]`, `> [!WARNING]`).
- Diagrams are fenced blocks tagged `mermaid`.
- Heading anchors are supported.
- Images are standard markdown images with relative paths into `docs/`. Raw HTML is stripped by the sanitizer. An image at an external URL will not load without a network, so docs that need to work offline keep their images in the library.
- Video: a relative link to an `.mp4` or `.webm` file renders as a `<video>` element with controls. External video (YouTube, Vimeo) is a plain link, because the sanitizer strips iframes.
- Content tabs are not in v1. Authors use headings or separate sections. If tabs come back, the syntax is chosen by three criteria: it renders in the editor, reads cleanly as raw text, and degrades on GitHub.
- Links between pages are relative `.md` paths (`../guides/color-management.md#aces`). These work on GitHub and in the editor, which opens them in the panel.
- `node:LoadImage` is an editor shorthand for a link to a node page, so it survives moving files around. An unqualified key means a node in the same library. A qualified form for another library is a later addition. GitHub does not render `node:` links, so an author who wants a link that works there uses a relative path.

Frontmatter is optional on library pages and node pages. Unknown keys are ignored so the set can grow.

```yaml
---
title: Create Empty Latents                                    # the title shown in the panel and the page tree
description: One or two sentences on what this page covers.   # defaults to the manifest description for nodes
when_to_use: Use this node instead of X when Y.                # guidance for agents
---
```

- The title is the frontmatter `title` when there is one. Without it, a node page uses the manifest `display_name`, and a library page uses its first `# H1`, then its filename. The scaffold writes no H1 on node pages, so the panel header shows the title and the page never repeats it.
- Agents read `description` and `when_to_use` to pick which pages to open.

### Node page template

A scaffold generates two sections for a new node page:

```markdown
## When to use it

## Example
```

- The docs check flags a section that is empty.
- An example is a workflow node in the library. The "Example" section links to it with `node:ShoutText`, and the user drags that node in like any other. The example workflow has its own node page.
- "When to use it" is the natural source of agent guidance. `when_to_use` is optional, and when it is absent an agent reads this section.
- Authors do not hand-write parameter lists. The generated table covers them, and longer parameter explanations go in a `## Parameters` section.

### Generated content and fallback

The engine builds a parameter table for each node: name, type, default, tooltip, whether it is an input, property, or output, the choices of a dropdown, and the range of a number. The editor shows the author's markdown, then the table.

- The table comes from instantiating the node at its default state. Many nodes add parameters at runtime, and those are not in it.
- `GetNodeDocumentationRequest` takes an optional node name. With one, the table reflects that node's current parameters, so the panel for a selected node on the canvas shows what the user sees.
- A `## Parameters` section in the page holds a `### <parameter_name>` heading per parameter, with a longer explanation under each. The editor merges each explanation into that parameter's row in the table. A heading that matches no parameter shows as ordinary page text, which covers parameters added at runtime.
- If no markdown file exists, the panel shows the node's manifest description and the table. The table makes tooltips the minimum docs, so the authoring guide includes a short style guide for tooltips: sentence case, and say what the value does.
- A node with `deprecation` metadata in the manifest shows a banner with its message and removal version, above the page.

### Engine API

```
GetNodeDocumentationRequest(node_name: str | None, node_type: str | None, library: str | None, offset: int, limit: int | None)
  -> GetNodeDocumentationResultSuccess(
       library, node_type, title,
       markdown: str | None,          # author content, None if no file
       total_chars: int, truncated: bool,
       description: str | None,
       when_to_use: str | None,
       deprecation: NodeDeprecationMetadata | None,
       parameters: list[ParameterDescription],   # same form as DescribeNodeTypeRequest
       assets_root: str | None,       # absolute docs dir, for resolving images
       external_url: str | None,      # optional "more docs" link from the manifest (not wired yet)
     )
ListLibraryDocumentationRequest(library: str)
  -> overview_path, pages (flat list in display order, folders ahead of their pages),
     nodes, templates, skills, tools   # tools: the MCP tools the library exposes (name, description, read_only, input_schema). A library can have tools and no nodes.
                                # nodes and templates carry page_path (relative to the docs folder, None with no page)
GetLibraryDocumentationPageRequest(library: str, path: str, offset: int, limit: int | None)
  -> title, markdown, total_chars, truncated, description, when_to_use, assets_root
```

- A node request names either `node_name` (a node on the canvas) or `node_type`. With `node_name`, the type and library come from the node, and `parameters` are the node's own, so two nodes of the same type can report different parameters. With `node_type`, `parameters` are the type's defaults. If both are given they must agree, and if neither is given the request fails.
- A template entry also carries `image`, the thumbnail from the template file's own metadata, and takes its `description` from that metadata when it has no page.
- `page_path` on a node or template entry is the path of its page relative to the docs folder, in the form the page request takes. A relative link inside a page resolves against the page it sits in, and the editor matches the result against these paths to open the right node or template.
- `offset` and `limit` read a long page in parts. `total_chars` is the whole page and `truncated` says whether the text stops early.
- The docs are read from the installed library on disk and served by the engine to the editor, so nothing leaves the machine. Reading docs needs no network, which makes them usable in air-gapped environments.
- Markdown travels in the response, so remote engines work.
- The engine builds its docs index when a library loads: which node pages exist, the page tree from the folder hierarchy, and the problems described above. Choosing Refresh Libraries (or restarting the engine) loads the libraries again and rebuilds the index, so a new, renamed, or deleted doc file shows up after a refresh. The index also holds each page's title and description for the lists, read once per load so that listing a library costs no file reads however many people ask. The text of a page is read from disk on each request, so an edit to a page's text shows up the next time the panel opens it, with no refresh. An edit to a title or description waits for a refresh, because the lists hold it.
- Paths are canonicalized with `canonicalize_for_identity` and must stay inside the docs folder. A key containing `..` or a separator fails the request.
- A markdown page over 1 MB is rejected, because the page travels in the response. The engine limits nothing else.
- Images: the editor mints preview URLs from `assets_root` plus the relative path with `CreateStaticFileDownloadUrlFromPathRequest`, as it does for other files. The engine never stores static URLs. Video is served with range requests so seeking works without loading the whole file.
- File reads go through the existing `ReadFileRequest`.
- There is no search request in v1. The editor already searches nodes, and agents read the list response as an index.

### Agents

Agents read the same docs as people, through the same requests.

- The list, get-node, and get-page requests are exposed as MCP tools alongside the existing ones.
- The list response is an `llms.txt`-style index: each page's path, title, `description`, and `when_to_use`. An agent reads the index, then fetches only the pages it needs.
- Everything returned over MCP is untrusted content, because it comes from the library author. Tool descriptions say so, and an agent treats docs as information and never as instructions to follow.
- MCP responses are capped at 64 KB, well below the 1 MB page limit. A longer page is cut with a truncation marker, and the tool takes an offset to read the rest.
- Responses return raw markdown with no editor-specific markup. The generated parameter table is also returned as structured data, so an agent reads names, types, defaults, choices, and ranges without parsing markdown.
- The sidebar agent can use library docs and skills. It discovers each library's `docs/.agents/skills/` folder in addition to the workspace's `.agents/skills/`, so one loader scans both.
- Skills are optional. A skill is `docs/.agents/skills/<name>/SKILL.md`. Node pages say what something is, and skills say how to accomplish a task with the library. The list response returns skills flagged as such, with their `description`. The body is fetched on demand.
- A library's skills come in two kinds, and the folder tells them apart. Skills for the agent that uses the library go in `docs/.agents/skills/`. Skills for agents that work on the library's code, like how to run the tests or follow the node doc format, go in `.agents/skills/` at the repo root, where coding agents already look. The sidebar agent never loads that folder, so a developer skill can't leak into a user's session.
- The authoring guide asks authors to write the first paragraph of each page as a standalone summary and to keep `description` to one or two sentences.

### Editor

- A docs panel opens from the node book icon and from the selection toolbar. It renders the page and then the parameter table, with the deprecation banner above the page.
- The panel builds its navigation from whatever the library ships. A library with guides or reference pages shows them as a page tree that follows the library's folders, and more pages mean more entries. The library's nodes always appear below, grouped by category. A library with only node docs, or none at all, shows just its nodes. There is no setting for any of this. A node page has a breadcrumb back to its library.
- Library Management gets a "Documentation" entry that opens the library's home page (`index.md`, or the page tree if there is none). Both work without a network, because the docs come from the installed library.
- The renderer supports GFM, GitHub alerts, Mermaid, and video. Mermaid loads lazily and runs in strict security mode.
- GUI PR #3394 becomes the entry point. `documentation_url` stays as an optional "More docs" link at the bottom of the panel. The git-remote parsing is not needed.

### Authoring loop and docs check

A `docs check` command runs in the author's terminal and in the library's CI, with the same checks the engine runs at load time. It reports:

- orphaned doc files and conflicting files for one node key
- a node name that differs from a file name only by case
- empty template sections
- missing tooltips
- files over 5 MB for an image, 25 MB for a video, or 100 MB for the whole docs folder, as warnings that do not fail the check
- broken image and page links
- external image URLs, which will not load without a network
- skills that name nodes the library does not have

The engine reports the same problems as library problems when it loads a library, so a renamed class that orphans its doc shows up in the editor without anyone running the command.

### Migration

The standard library is the first library to move.

- A script moves the 166 pages in `docs/nodes/` and `docs/libraries/` into the standard library repo. It converts the 19 `!!!` admonitions in 9 pages to alerts (the 4 `example` blocks become plain blockquotes) and rewrites the 244 relative links.
- Existing page filenames are not class names. The diffusers page `empty_latents.md` documents a node titled "Create Empty Latents", so the script matches each page to a node by its H1 against the manifest's `display_name`, renames the file to the class name, and moves the old H1 into frontmatter `title` when it differs from the display name. A person checks the pages it cannot match.
- Images become plain markdown images. The 18 `<img width="480">` tags are node screenshots of 578 to 1214 pixels, and the panel scales an image to its width, so the sizing attribute is not needed.
- The diffusers pages carry content the generated output now replaces, which the script drops: the `Category:` line (the manifest has it), the `Inputs` and `Outputs` tables (the generated table covers names, types, and defaults), and the hand-written node list in the library index (the editor groups nodes by manifest category). Per-parameter notes in those tables move into a `## Parameters` section. The node preview screenshots stay as ordinary images.
- Pages that mention a workflow template by path, such as "Workflow template: `workflows/templates/ControlnetText2Image.py`", become a `node:` link when the template is a workflow node, and plain text when it is not.
- The main site's node pages are replaced by a libraries index, and the old URLs redirect to it.
- `documentation_url` stays supported. A library with no `docs/` folder still gets its "More docs" link, so libraries that host their own docs lose nothing.
- Library docs are not mirrored to the main site, because a build-time copy goes stale between site rebuilds and cannot match the version a user has installed.

## Phasing

The order in which this feature is built. Each phase is a stage of development work, not a release or a version of the docs format. Later phases build on earlier ones.

1. Engine and editor panel: manifest field, request handlers, the parameter table, MCP exposure, load-time library problems, the `docs check` command, a panel that shows a node's page and table and a library's pages (home page, guides, reference), tests, authoring docs page, changelog entry.
1. Editor: alerts, Mermaid, video, deprecation banner, library skill discovery for the sidebar agent.
1. Scaffold command, the standard library migration, locales.

## Decisions

- A library can ship its own pages (a home page, guides, reference) as well as per-node pages, and users can read both in the editor in the first release.
- Navigation is the folder hierarchy, with no `nav.yml` and no navigation in the manifest. Order is alphabetical by filename, and number prefixes on filenames change it. There is no per-page `order` field.
- Docs are local files in the installed library, read through the engine, so they work in air-gapped environments.
- Library docs are not mirrored to the main site.
- No raw HTML, no tabs, and no search request in v1. The engine's only size rule is the 1 MB page limit, and the docs check warns about large files. Frontmatter is limited to `title`, `description` and `when_to_use`.
- Docs problems are library problems and never stop a library from loading.
- The manifest has no per-node documentation setting. A node's page is found by its filename, and nothing in the JSON names a doc file. A path in the JSON can drift from the files and go stale, and the convention keeps the format simple for node authors and for agents.
- MCP responses are capped at 64 KB per call.

## Open questions

None at the moment.
