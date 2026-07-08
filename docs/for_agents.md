# For Agents

This page documents the machine-readable surface that
[docs.griptapenodes.com](https://docs.griptapenodes.com/) exposes for AI
coding agents, MCP skills, and any tool that wants to ground its work in the
engine's actual API.

The same files that render this site are also published as post-processed
markdown so an agent can fetch them directly. Snippets, macros, and
mkdocstrings output are already expanded, which `raw.githubusercontent.com`
does not give you.

## Surface

- [`/llms.txt`](https://docs.griptapenodes.com/en/stable/llms.txt) is the curated
    index per the [llms.txt convention](https://llmstxt.org/). It groups the
    high-value pages (scripting, the project system, custom node development,
    MCP integration, and the node reference) under named sections with short
    descriptions and absolute URLs.
- [`/llms-full.txt`](https://docs.griptapenodes.com/en/stable/llms-full.txt) is the
    full concatenated post-processed markdown of every page in the nav. Use
    this when you want to drop the entire engine docs into a single context
    window.
- Every doc page is also available as standalone markdown next to its HTML
    rendering. The URL is the rendered page URL with `/index.md` appended,
    for example
    [`/developing_nodes/comprehensive_guide/index.md`](https://docs.griptapenodes.com/en/stable/development/custom_nodes/comprehensive_guide/index.md)
    and [`/retained_mode/index.md`](https://docs.griptapenodes.com/en/stable/development/retained_mode/index.md).
    Top-level pages live at `/<page>/index.md`; nested pages mirror the
    rendered URL.

## When to use which

- Reach for **`/llms.txt`** to discover what's available without pulling the
    whole corpus. It's small enough to skim and the section descriptions tell
    you which page covers which topic.
- Reach for **`/llms-full.txt`** when you want a single-shot grounding
    document and have the context budget for it. It is the same content as
    the per-page markdown files, concatenated in the order declared in the
    llms.txt sections.
- Reach for **a single per-page `.md`** when you already know which page
    you need (e.g. you're writing a custom node and want the comprehensive
    guide, or you're scripting and want `retained_mode.md`).

## High-value pages for engine grounding

If you are pointing an agent at a small set of pages to bootstrap, these
five cover most of the engine's first-party API surface:

- [Scripting (retained mode)](https://docs.griptapenodes.com/en/stable/development/retained_mode/index.md)
- [Comprehensive node development guide](https://docs.griptapenodes.com/en/stable/development/custom_nodes/comprehensive_guide/index.md)
- [Getting started with node development](https://docs.griptapenodes.com/en/stable/development/custom_nodes/getting_started/index.md)
- [Project system overview](https://docs.griptapenodes.com/en/stable/guides/projects/index.md)
- [MCP integration overview](https://docs.griptapenodes.com/en/stable/guides/mcp/index.md)

## Stability

- The surface tracks `main` via the existing docs deploy pipeline. There is
    one live surface; versioned URLs (e.g. `/v0.40/llms.txt`) are not
    currently published.
- The set of pages is driven by the `llmstxt` plugin block in `mkdocs.yml`.
    Adding a new doc page does not automatically include it in `/llms.txt` or
    `/llms-full.txt`; the page must be added under a section there. Reference
    pages under `nodes/` are picked up via a glob.
