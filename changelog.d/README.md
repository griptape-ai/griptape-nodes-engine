# changelog.d

One unreleased changelog entry per file, so two pull requests adding entries never touch the same
line of the same file and never conflict. `CHANGELOG.md` holds released history; the files here are
what the next release adds to it.

At release time `scripts/changelog.py roll` folds these files into the new version section and
deletes them.

## Adding an entry

Create `<type>-<slug>.md`, where `<type>` is one of `added`, `changed`, `deprecated`, `removed`,
`fixed`, or `security`, and `<slug>` is a few lowercase words joined by hyphens:

```text
changelog.d/fixed-workflow-dir-warnings.md
```

The file holds the bullet exactly as it should read in `CHANGELOG.md`, and nothing else:

```markdown
- Saving a file from a workflow you have not saved yet no longer logs a stream of "Optional builtin
  'workflow_dir' could not be resolved" warnings. `workflow_dir` now answers with the folder your
  first save would default to, read from the project's `save_workflow` situation.
  [#5669](https://github.com/griptape-ai/griptape-nodes-engine/issues/5669)
```

`make check/changelog` enforces the shape:

- the filename names one of the six change types
- nothing else lives here: every file is an entry, named `<type>-<slug>.md`
- the file starts with `- ` and holds exactly one entry
- the file is UTF-8 text
- continuation lines are indented two spaces
- no headings, since the type is in the filename
- `## [Unreleased]` in `CHANGELOG.md` carries no entries of its own; they all live here
- these files fold into a `CHANGELOG.md` that still parses

Wrap at about 100 characters. Nothing checks that, and nothing reformats it either: `mdformat`
skips this directory, so the wrapping you write is the wrapping that ships.

What belongs in an entry, and how to word it, is in [CLAUDE.md](../CLAUDE.md#changelog) and
[CONTRIBUTING.md](../CONTRIBUTING.md#changelog). Refactors, tests, CI, and docs-only changes get no
entry at all.

## Previewing what the release will say

```shell
make changelog
```

That prints the `[Unreleased]` section as the next release will show it: these files folded in,
change types in Keep a Changelog order, breaking changes first within each type.
