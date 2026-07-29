# Sequences

Griptape Nodes can read directories of numbered files as **sequences** — for example, a render output like `render.0001.exr`, `render.0002.exr`, … `render.0100.exr`, but also dialogue takes (`take_##.wav`), text chunks (`chapter_###.md`), or anything else where a numeric key in the filename groups items into an ordered set. You point a sequence-aware node at a *path or pattern* (a filename with a placeholder for the number, or just a literal file path), and the engine finds the matching items on disk, handles gaps, and gives you back a list of entries with their integer numbers and zero-padded string forms.

This page explains the path/pattern syntax, the policies for handling missing items, and the conventions you should know before authoring one.

## Pattern syntax

A sequence pattern looks like a filename with a token in place of the item number. Four token forms are supported:

| Token  | Width | Notes                                                         |
| ------ | ----- | ------------------------------------------------------------- |
| `####` | 4     | Each `#` is one digit. `##` = 2-digit, `####` = 4-digit, etc. |
| `%04d` | 4     | C-style printf. `%04d` = 4-digit zero-padded.                 |
| `@@@@` | 4     | Houdini/RV style. Same meaning as `####`.                     |
| `$F4`  | 4     | Houdini variable. Same meaning as `####`.                     |

All four are equivalent; pick whichever matches your pipeline conventions. We recommend `####` or `%04d` for new templates — they're the most widely understood across DCC tools.

A few examples:

```
render.####.exr             item 5 → render.0005.exr
render.%04d.png             item 12 → render.0012.png
take_##.wav                 item 7 → take_07.wav
```

The token always sits in the **filename**. Tokens inside directory components (e.g. `render/####/beauty.exr`) are not supported — keep the number in the filename portion only.

## When the path has no sequence token

A path with no sequence token (`/work/photo.png`, `{inputs}/poster.png`, `render.0002.png`) is *ambiguous*. The artist might mean:

1. **the literal name of one specific file** — `render.0002.png` is the file I want;
1. **one frame of an implicit sequence** — fileseq sees the digits and groups every `render.NNNN.png` it finds into a single sequence;
1. **an oversight** — the artist forgot to type `####`, and the right answer is to fail loudly so they fix it.

The engine asks the caller which interpretation to use via `NoTokenBehavior` (in `griptape_nodes.common.sequences`):

| Value                     | What you get                                                                                                                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SINGLE_FILE` *(default)* | Treats the whole filename as a literal. A 1-item sequence (`first=last=1, padding=0`) when the file exists; an empty result when it doesn't. Sibling files in the same directory are ignored — `render.0002.png` returns just `render.0002.png` even if `0001..0005` are next to it. |
| `EXPLORE_SEQUENCE`        | Lets fileseq read digits in the filename as an implicit sequence token. `render.0002.png` becomes one frame of an inferred `render.####.png` sequence; the scan walks every matching sibling. Useful when a downstream tool gave you one filename but you want the whole take.       |
| `REJECT`                  | Fails fast with `INVALID_TEMPLATE` and a message asking the artist to add a token. Strict mode for pipelines that should never silently widen the artist's intent.                                                                                                                   |

Defaulting to `SINGLE_FILE` keeps the path → sequence-of-one mapping that "I picked one file" intuitively expects. Workflows that want the implicit-grouping behavior opt in explicitly.

## Combining with macros

Sequence paths work alongside the project's [macro language](macros.md). The macro head is resolved internally so the engine can read the right directory off disk, but the engine emits paths in the shape you supplied:

```
input  : {inputs}/shot_a/render.####.exr
output : Sequence(directory="{inputs}/shot_a",
                  entries=[{path: "{inputs}/shot_a/render.0001.exr"},
                           {path: "{inputs}/shot_a/render.0002.exr"}, ...])
```

This is what makes scan results portable. A workflow built on a machine where `{inputs}` resolves to `/Volumes/Renders` produces sequences whose paths still say `{inputs}` — re-opening that workflow on a machine where `{inputs}` resolves to `C:\renders` still works, because every consumer downstream resolves the macro fresh against its own project.

Plain absolute paths (no `{...}` segments) round-trip identically — you put `/work/render.####.png` in, you get `/work/render.0001.png` back.

**Relative paths.** A path with no leading `/` and no macro head — e.g. `shot_a/render.####.png` — is interpreted relative to the project's workspace directory. The engine prepends the workspace path before listing, so `shot_a/render.####.png` and `{workspace_dir}/shot_a/render.####.png` resolve identically. Use whichever form reads more naturally for the workflow.

Macro variables inside `{...}` are completely separate from sequence tokens — they don't share syntax, and they're resolved at different stages.

## Width matching is strict

The number of `#` characters (or the `%0Nd` width) declares the **exact** number of digits to match. If your pattern says `####`, the engine matches files with exactly 4 digits in the slot — `render.0001.exr` matches, but `render.001.exr` (3 digits) and `render.12345.exr` (5 digits) do not.

This matches what Nuke does. If your sequence has numbers that overflow the declared padding (e.g., a 4-digit pattern but real numbers go above 9999), use a wider pattern (`#####`) to capture them.

If a directory contains files with mixed padding widths — for example both `render.0001.png` and `render.001.png` — they'll be treated as **separate sequences**. The engine matches the one whose padding matches your declared template; the others are silently ignored.

## Missing-item policies

Real sequences often have gaps — a render that crashed on frame 47, a sparse export that only saved every other take, a chapter that's still unwritten. When you scan a sequence, you choose a **policy** for how those gaps are handled:

| Policy              | What you get                                                                                                                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ABORT`             | Fail fast. Surfaces a failure carrying the offending item number on the first gap inside `[first, last]`. No Sequence is returned.                                                                |
| `SPLIT` *(default)* | One sequence per **contiguous run** of present items. A sequence with items 1–5, 8–12, 15 produces three separate sequences.                                                                      |
| `SKIP`              | One sequence containing only the present items. Gaps are absent from the output (but visible via the sequence's `missing_numbers` set).                                                           |
| `FILL_NEAREST`      | One sequence covering the full `[first, last]` range. Each missing item is filled with the path of the nearest **earlier** present item (or the nearest later one if no earlier neighbor exists). |

Pick `ABORT` when a gap should fail the workflow loudly (e.g. a render that crashed must not silently advance). Pick `SPLIT` when you want to *preserve* the gap structure (each contiguous run is meaningful on its own). Pick `SKIP` when you want a single sparse sequence, or `FILL_NEAREST` for a single dense sequence, regardless of what's actually on disk.

**Domain-specific gap rendering belongs to nodes, not the engine.** If you want a black-frame placeholder, a magenta/yellow checkerboard, a silent audio chunk, an empty text chunk, or anything else synthesized in place of a missing item, scan with `SKIP` and walk `missing_numbers` in your node — render whatever your domain calls for. The engine deliberately stops at "this number isn't on disk"; the node owns the rest.

## Subset clipping

Sequence-aware nodes accept optional `start` and `end` bounds. When supplied, the scan is clipped to that range:

- Items below `start` and above `end` are dropped from the output.
- The original disk range is still reported via `discovered_first` / `discovered_last`, so you can see what existed before the clip.
- Subset bounds outside the discovered range yield an empty result (Failure).

## What you get back

`Sequence` is a Pydantic model — read fields by attribute (`seq.first`, `seq.entries[0].number`). Nodes that operate on sequences should declare their input as `type="Sequence"`; the engine validates the connection by name.

Each `Sequence` carries:

- **`first`** / **`last`** — the active range (after subset clipping).
- **`discovered_first`** / **`discovered_last`** — what was actually on disk, ignoring any subset.
- **`padding`** — the declared zero-padding width (e.g. 4 for `####`).
- **`pattern`** — the canonical pattern (e.g. `render.####.exr`).
- **`directory`** — the directory portion of the path you supplied, in the same shape (macro-form when you supplied a macro, plain absolute otherwise).
- **`policy`** — which policy was applied.
- **`entries`** — one `SequenceEntry` per item in the active range. Each has:
    - `number` — the integer key (e.g. 5).
    - `padded_number` — the zero-padded form (e.g. `0005`).
    - `path` — the file path as a string, in the same shape you supplied. A macro-form input round-trips with the macro head intact (`{inputs}/render.0005.exr`); a plain absolute input round-trips identically (`/work/render.0005.exr`). Under `FILL_NEAREST`, gap entries carry the nearest present neighbor's path; cross-check `entry.number in seq.present_numbers` to tell present from filled.
- **`present_numbers`** — the set of numbers actually on disk inside `[first, last]`.
- **`missing_numbers`** — derived from `present_numbers`: the numbers in the active range that aren't on disk (useful for diagnostics under any policy).

## Cases that are intentionally not supported

A few cases are deliberately excluded:

- **Negative numbers**: files like `render.-0005.exr` are filtered out at scan time. The dropped count is reported on the resulting sequence.
- **Sequence tokens in directory components**: patterns like `render/####/beauty.exr` aren't matched. Put the number in the filename.
- **Multi-token patterns**: templates with more than one sequence token (e.g. `v##_f####.exr`, `render.##.##.exr`) are rejected at scan time with a clear error. Use a single token per pattern.
- **Time codes**: not yet supported.

## Where this comes from

Sequence handling is built on [`fileseq`](https://github.com/justinfx/fileseq), the de facto Python library for VFX-style frame-range parsing. We use it as a parser and number-math library; all filesystem listings still flow through the engine's request bus, so the same workspace permissions, path normalization, and Windows-long-path handling that govern other file operations apply here too. fileseq itself uses "frame" terminology throughout — that's an implementation detail; the public API speaks "items" and "numbers" so the same code can serve any numbered-filename sequence, not just images.

## Public entry point: `ScanSequencesRequest`

Scans are dispatched on the engine's event bus, not by importing a function. Send a `ScanSequencesRequest` (defined in `griptape_nodes.retained_mode.events.os_events`) and `await GriptapeNodes.ahandle_request(...)`; the handler resolves any project macros, runs the directory listing, and runs fileseq parsing in a worker thread, so a long-running scan over a deep directory doesn't block the event loop.

The request takes a single `path` field. Examples:

```python
# Macro-form pattern. The {inputs} head is preserved on every emitted path.
ScanSequencesRequest(path="{inputs}/shot_a/render.####.exr")

# Plain absolute pattern. Round-trips identically.
ScanSequencesRequest(path="/work/render.####.png", policy=MissingItemPolicy.SKIP)

# Token-less path. Default `no_token_behavior=SINGLE_FILE` returns a one-item
# sequence containing exactly that file (or empty if it doesn't exist).
ScanSequencesRequest(path="/work/photo.png")

# Token-less path, but treat it as one frame of an implicit sequence and
# walk every matching sibling.
ScanSequencesRequest(
    path="/work/render.0002.png",
    no_token_behavior=NoTokenBehavior.EXPLORE_SEQUENCE,
)

# Strict mode: fail with INVALID_TEMPLATE if the path has no token.
ScanSequencesRequest(
    path="/work/render.0002.png",
    no_token_behavior=NoTokenBehavior.REJECT,
)

# Active range subset.
ScanSequencesRequest(path="{inputs}/render.####.exr", start_number=10, end_number=50)
```

Success returns `ScanSequencesResultSuccess` carrying:

- **`sequences: list[Sequence]`** — the inferred sequences, post-policy. Every `Sequence.directory` and `entry.path` keeps the same path shape you supplied (macro-form in, macro-form out).
- **`has_entries: bool`** — true iff at least one Sequence has at least one entry. A scan that ran cleanly but found nothing is *success with `has_entries=False`*, not failure — callers that need to fail-fast on empty results check `has_entries` themselves.
- **`directory_had_matching_files: bool`** — true iff the directory listing produced at least one file whose basename + extension matched the target shape (i.e. the prefilter accepted something). Combined with `has_entries`, it tells you *why* a scan came up empty: false means the path is wrong or the basename/extension doesn't match anything; true with `has_entries=False` means the files are there but the padding doesn't line up *or* the active subset clipped them all out.
- **`discovered_first: int | None`** / **`discovered_last: int | None`** — the on-disk range of inferred numbers *before* subset clipping is applied. Populated whenever fileseq inferred at least one number from the directory; `None` if the listing yielded no padding-matching numbers. Lets the caller diagnose subset-clip cases without guessing — e.g. "asked for 90..100 but disk has 1..7" comes straight from these fields.

These three diagnostic fields let consumers distinguish wrong-path / wrong-padding / wrong-range cases without inspecting `result_details` strings. Per-Sequence `discovered_first`/`discovered_last` (on each `Sequence` object) are still the right read when you have at least one sequence; the top-level fields are specifically for the empty-result diagnostic.

Failure returns `ScanSequencesResultFailure` whose `failure_reason` is either a `SequenceScanFailureReason` (`INVALID_TEMPLATE`, `INVALID_BOUNDS`, `ABORTED_AT_GAP`) or an OS-layer `FileIOFailureReason`. Failures are reserved for cases where the scan couldn't proceed:

- `INVALID_TEMPLATE` — the path string couldn't be parsed (bad macro syntax, multi-token pattern, missing filename component, unresolvable macro variables, etc.). Also surfaces when the path has zero sequence tokens and `no_token_behavior` is `REJECT`.
- `INVALID_BOUNDS` — `start_number < 0` or `end_number < start_number`.
- `ABORTED_AT_GAP` — the `ABORT` policy hit at least one gap inside the active range. The failure populates `missing_item_numbers: list[int]` with every offending integer key, sorted ascending — UI consumers can show the artist *all* the missing slots in one pass instead of fixing them one re-run at a time.
- A `FileIOFailureReason` value — the inner directory listing failed (directory not found, permission denied, etc.). These surface via `FileIOFailureReason`, not folded into empty success.

### Node-level: `fail_on_empty_result`

The standard library's `ScanSequenceNode` and `ScanSplitSequenceNode` both expose a top-level `fail_on_empty_result: bool = True` parameter. When true (the default), an empty scan result routes the node through its Failure control-flow edge with a diagnostic-aware status message built from the fields above. When false, the node succeeds with empty outputs and a status noting the opt-out — for workflows that legitimately tolerate empty scans (e.g. a sweep that may find nothing).

### Node-level: *When there's no sequence marker*

Both Scan nodes also expose the `NoTokenBehavior` choice as a friendly dropdown labelled **When there's no sequence marker (e.g., ###)**, inside the collapsed *Advanced Sequence Control* group. The three options map onto the engine enum:

| Dropdown label                     | Engine value       |
| ---------------------------------- | ------------------ |
| *Treat as a single file* (default) | `SINGLE_FILE`      |
| *Treat as part of a sequence*      | `EXPLORE_SEQUENCE` |
| *Fail unless a token is present*   | `REJECT`           |

The same dropdown also drives the relative-bounds discovery probe inside the node (when *Start at* / *End at* is set to *Relative …*), so a literal-file path doesn't get explored just because the node is sniffing for offsets.

Library and node code should never import the underlying scanner directly — the request bus is the only public path.
