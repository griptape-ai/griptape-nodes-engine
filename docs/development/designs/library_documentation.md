# Design: user-authored node documentation in libraries

Tracking issue: [griptape-ai/internal#222](https://github.com/griptape-ai/internal/issues/222).

> **Design iteration only.** This page exists for the team to comment on and revise. It is not part of the published docs, so it is deliberately left out of `mkdocs.yml`. Do not add it to the nav or the `llmstxt` sections.

## Goals

- A library author writes node and library docs in their own repo with no hosting, no build step, and no PR to the engine.
- Users read the docs inside the editor, fast, offline, and matching the installed library version. The docs are local files in the installed library, so they work in air-gapped environments with no network.
- Agents read the same docs through the engine.
- Every node has useful help even if the author writes nothing.

## Proposal

### Location and file layout

Docs live in a `docs/` folder next to `griptape_nodes_library.json`. The manifest's folder is the library root, whether the manifest sits at the repo root or inside a package subfolder. It is the one location the engine already resolves paths from.

In the layout below, only `docs/nodes/` has a fixed meaning: it holds one page per node. Everything else under `docs/` is library-level docs, meaning pages about the library as a whole and not about one node. `guides/` and `reference/` are made-up examples of those. A library can name and arrange them any way it likes, or have none.

```
my-library/
  griptape_nodes_library.json
  .agents/                    # for agents only, same convention as the workspace
    skills/
      color-managed-workflow/
        SKILL.md              # name + description frontmatter, same format as other skills
  nodes/                      # Python source; the folders here are unrelated to the docs folders
    audio/
      load_audio.py           # defines class LoadAudio
      mix_audio.py            # defines class MixAudio
    image/
      load_image.py           # defines class LoadImage
  workflows/
    shout_text.py             # saved workflow, registered as the node type "ShoutText"
  docs/
    index.md                  # library home page (optional)
    guides/                   # EXAMPLE of library-level docs: any folder name works, none are required
      getting-started.md      # not about a specific node
      color-management.md     # not about a specific node
    reference/                # EXAMPLE of library-level docs: any folder name works, none are required
      file-formats.md         # not about a specific node
    nodes/                    # the one fixed folder: one page per node, matched by node key
      audio/
        LoadAudio.md          # documents the node LoadAudio
        LoadAudio.fr.md       # French version of the same page (locales are later, the name is reserved now)
        MixAudio.md           # documents the node MixAudio
      LoadImage.md            # sits at the top of docs/nodes/, still documents LoadImage
      ShoutText.md            # documents the workflow node ShoutText
    assets/
      screenshot.png
```

The `nodes` and `workflow_nodes` entries in `griptape_nodes_library.json` that these files document:

```json
{
  "nodes": [
    { "class_name": "LoadAudio", "file_path": "nodes/audio/load_audio.py", "metadata": { "...": "..." } },
    { "class_name": "MixAudio",  "file_path": "nodes/audio/mix_audio.py",  "metadata": { "...": "..." } },
    { "class_name": "LoadImage", "file_path": "nodes/image/load_image.py", "metadata": { "...": "..." } }
  ],
  "workflow_nodes": [
    { "node_type": "ShoutText", "workflow_path": "workflows/shout_text.py", "metadata": { "...": "..." } }
  ]
}
```

Node pages:

- A node's doc file is named after the key the manifest already uses: `class_name` for Python nodes and `node_type` for workflow nodes. No new mapping to maintain.
- Node docs may sit in nested folders under `docs/nodes/`. The engine indexes `docs/nodes/**/*.md` when it loads the library and matches each filename to a node key, so `docs/nodes/audio/LoadAudio.md` documents `LoadAudio`. The folders are the author's choice. They do not have to match the source folders or the manifest categories, which can change independently.
- The match is on the node key, not the Python filename. `load_audio.py` documents nothing by itself, and a source file that defines several node classes gets one doc page per class. Matching is exact and case-sensitive. The docs check flags a name that matches a node key except for case, because it would work on a case-insensitive filesystem and fail on another.
- Locale variants are named `<Key>.<locale>.md`. A name is split at its first dot, so `LoadAudio.fr.md` is the French page for `LoadAudio` and is never read as a node called `LoadAudio.fr`. A node key contains no dots. Locales are not in v1, and `<Key>.md` stays the fallback when they arrive.
- Two files that resolve to the same key (`audio/LoadAudio.md` and `legacy/LoadAudio.md`) are a conflict. The engine uses the file with the fewest folders, then the first path alphabetically, and reports a library problem that names both files. A docs problem never stops a library from loading. The docs check fails on it.
- A node with no file falls back to its manifest description and the generated parameter table. A file that matches no node is an orphan. The engine reports it as a library problem at load time, and the docs check fails on it. Changing a node's `display_name` keeps the same class and the same docs. Renaming the class itself changes the key, which also breaks saved workflows, so it is rare. The orphan report tells the author to rename the file, and the docs are not lost silently.
- Node keys are unique within a library, which the engine already enforces. Only doc files can collide.

Library pages:

- Everything under `docs/` outside the top-level `nodes/` folder is a library page. `nodes/` is reserved at the top level only, so `docs/guides/nodes/` is an ordinary page folder. A library page's title follows the title rule under File format.

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
GetNodeDocumentationRequest(library: str, node_type: str, node_name: str | None = None)
  -> GetNodeDocumentationResultSuccess(
       markdown: str | None,          # author content, None if no file
       description: str | None,
       when_to_use: str | None,
       deprecation: ... | None,
       parameters: list[ParameterDoc],
       assets_root: str | None,       # absolute docs dir, for resolving images
       external_url: str | None,      # optional "more docs" link from the manifest
     )
ListLibraryDocumentationRequest(library: str)
  -> page tree: [{title, path, description, when_to_use, kind, children}], plus the node list grouped by category
GetLibraryDocumentationPageRequest(library: str, path: str)
  -> markdown, title, assets_root
```

- The docs are read from the installed library on disk and served by the engine to the editor, so nothing leaves the machine. Reading docs needs no network, which makes them usable in air-gapped environments.
- Markdown travels in the response, so remote engines work.
- The engine builds its docs index when a library loads: which node pages exist, the page tree from the folder hierarchy, and the problems described above. Choosing Refresh Libraries (or restarting the engine) loads the libraries again and rebuilds the index, so a new, renamed, or deleted doc file shows up after a refresh. The text of a page is read from disk on each request, so an edit to an existing page shows up the next time the panel opens it, with no refresh.
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
- The sidebar agent can use library docs and skills. It discovers each library's `.agents/skills/` folder in addition to the workspace's, so one loader scans both.
- Skills are optional. A skill is `.agents/skills/<name>/SKILL.md` at the library root, outside `docs/`, so `docs/` has no folder reserved for agents. Node pages say what something is, and skills say how to accomplish a task with the library. The list response returns skills flagged as such, with their `description`. The body is fetched on demand. `.agents/` is the home for any other agent-only content later. The folder is hidden in Finder and `ls`, so the scaffold command creates it and the authoring guide names it.
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
