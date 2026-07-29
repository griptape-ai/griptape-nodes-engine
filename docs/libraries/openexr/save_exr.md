# Save EXR

**Saves a single-part EXR file from either an 8-bit image or EXR channel artifacts, preserving float precision in channel mode.**

Category: `EXR`

## TL;DR

- Two modes: **Mode A** takes an `ImageArtifact`/`ImageUrlArtifact` (8-bit) and normalizes [0, 255] to [0.0, 1.0]; **Mode B** takes up to four `EXRChannelArtifact` slots and preserves float precision.
- Mode B takes priority: `image_in` is ignored whenever any channel slot is connected.
- Output includes an `EXRPartArtifact` descriptor for the written part, so you can chain straight into [Display EXR Part](display_exr_part.md).
- A collapsed **Metadata** group exposes optional header fields (owner, timecode, comments, custom attributes, ...).

## Typical workflow position

```text
(image node) → [Save EXR] → Display EXR Part / Load EXR
Load EXR (channels) → [Save EXR]
```

## Node preview

<!-- TODO: add ../assets/save-exr.png screenshot -->

## Inputs

| Name        | Type                                | Required | Notes                                                                    |
| ----------- | ----------------------------------- | -------- | ------------------------------------------------------------------------ |
| `image_in`  | `ImageArtifact \| ImageUrlArtifact` | No       | 8-bit image source (Mode A). Ignored when any channel slot is connected. |
| `channel_r` | `EXRChannelArtifact`                | No       | Channel to write as R (Mode B).                                          |
| `channel_g` | `EXRChannelArtifact`                | No       | Channel to write as G (Mode B).                                          |
| `channel_b` | `EXRChannelArtifact`                | No       | Channel to write as B (Mode B).                                          |
| `channel_a` | `EXRChannelArtifact`                | No       | Channel to write as A (Mode B).                                          |

## Outputs

| Name          | Type              | Notes                                                                                                                          |
| ------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `output_file` | `str`             | Path to the written `.exr` file.                                                                                               |
| `output_part` | `EXRPartArtifact` | Descriptor for the written part — connectable to [Display EXR Part](display_exr_part.md) or [Load EXR](load_exr.md) consumers. |

## Parameters

| Name          | Type                                 | Default | Notes                                                                |
| ------------- | ------------------------------------ | ------- | -------------------------------------------------------------------- |
| `compression` | `ZIP \| ZIPS \| PIZ \| DWAA \| NONE` | `ZIP`   | Codec applied to the output file.                                    |
| `pixel_type`  | `HALF \| FLOAT`                      | `HALF`  | `HALF` is 16-bit float (standard); `FLOAT` is 32-bit full precision. |

### Metadata *(collapsed by default)*

| Name                 | Type    | Default | Notes                                            |
| -------------------- | ------- | ------- | ------------------------------------------------ |
| `part_name`          | `str`   | `""`    | Name for this EXR part.                          |
| `pixel_aspect_ratio` | `float` | `1.0`   | Pixel width/height ratio.                        |
| `owner`              | `str`   | `""`    | Asset owner.                                     |
| `comments`           | `str`   | `""`    | Free-text comments.                              |
| `capture_date`       | `str`   | `""`    | Capture date (e.g. `2025-01-01T12:00:00`).       |
| `software`           | `str`   | `""`    | Authoring application name.                      |
| `time_code`          | `str`   | `""`    | Editorial timecode (`HH:MM:SS:FF`).              |
| `custom_attributes`  | `str`   | `""`    | Non-standard header attributes as a JSON object. |

## Tips & pitfalls

- **Mode B wins.** If you meant to save `image_in` but a channel slot is still wired, the image is silently ignored — disconnect the channel slots.
- **8-bit sources don't become HDR.** Mode A normalizes display-referred 8-bit data into [0.0, 1.0]; it does not recover highlights. Use Mode B with float channels for real HDR data.
- **Pick `DWAA` for big beauty renders.** It's lossy but dramatically smaller; keep `ZIP`/`PIZ` for data passes (depth, normals) where exact values matter.
- **`FLOAT` doubles file size vs `HALF`.** Only use 32-bit when the extra precision is actually needed (e.g. depth or position passes).

## See also

- [Load EXR](load_exr.md) — provides channel inputs and re-reads written files.
- [Display EXR Part](display_exr_part.md) — preview the written part via `output_part`.
