# Load EXR Sequence

**Scans every frame of an EXR sequence and outputs an ordered list of part descriptors, without reading pixel data.**

Category: `EXR`

## TL;DR

- Takes a `Sequence` (from **Scan Sequence** or **Scan Split Sequence**) and outputs one `EXRPartArtifact` per frame, ordered by frame number.
- Connects directly into a **ForEach Start** loop for per-frame work such as [Display EXR Part](display_exr_part.md).
- Header-only: always reads just the frame headers, whatever the `openexr.header_only` setting says. Downstream nodes read pixels themselves.
- Reads the same part (`part_index`) from every frame.

## Typical workflow position

```text
Scan Sequence → [Load EXR Sequence] → ForEach Start → Display EXR Part → ForEach End
```

## Node preview

<!-- TODO: add ../assets/load-exr-sequence.png screenshot -->

## Inputs

| Name       | Type       | Required | Notes                                      |
| ---------- | ---------- | -------- | ------------------------------------------ |
| `sequence` | `Sequence` | Yes      | EXR frame sequence from **Scan Sequence**. |

## Parameters

| Name          | Type           | Default | Notes                                                                                                                                                     |
| ------------- | -------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `part_index`  | `int`          | `0`     | Zero-based EXR part index to scan in every frame.                                                                                                         |
| `on_mismatch` | `skip \| fail` | `skip`  | What to do when a frame fails to scan or doesn't match the sequence's resolution and channel layout. `skip` drops that frame; `fail` fails the whole run. |

### Advanced *(collapsed by default)*

| Name      | Type  | Default | Notes                                            |
| --------- | ----- | ------- | ------------------------------------------------ |
| `threads` | `int` | `4`     | Number of concurrent header-scan workers (1–16). |

## Outputs

| Name          | Type                    | Notes                                                                                                     |
| ------------- | ----------------------- | --------------------------------------------------------------------------------------------------------- |
| `parts`       | `list[EXRPartArtifact]` | One descriptor per scanned frame, ordered by frame number. May be shorter than `frame_count` with `skip`. |
| `frame_count` | `int`                   | Number of frames in the sequence.                                                                         |

## Tips & pitfalls

- **List position isn't the frame number.** Each artifact's `frame_number` field carries the real sequence frame number; with `skip`, dropped frames leave no gap in the list.
- **The most common layout wins.** Frames are checked against the resolution and channel layout shared by most of the successfully scanned frames, so an outlier such as a slate frame is the one dropped (or, with `fail`, the one reported). If no layout covers more than half the scanned frames, the node fails. Frames that fail to scan don't count toward that majority.
- **Single part per run.** For multi-part frames, add one node per `part_index` you need.

## See also

- [Read EXR](read_exr.md) — render one frame of a sequence directly.
- [EXR Sequence to Preview](exr_sequence_to_preview.md) — encode the whole sequence to a video.
- [Display EXR Part](display_exr_part.md) — render each frame's part inside the loop.
