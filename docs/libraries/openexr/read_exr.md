# Read EXR

**Reads an EXR file or one frame of a sequence and outputs a viewable image alongside the part and channel descriptors, in a single node.**

Category: `EXR`

## TL;DR

- Covers the common case of `Load EXR → Display EXR Part` in one node: point it at a file and it renders the selected part and layer to a PNG.
- Accepts a single `.exr` path, a frame pattern such as `render.####.exr`, or a connected `Sequence` (from **Scan Sequence**).
- Renders one frame per run. To iterate a sequence, connect **For Loop Start**'s `index` to `frame`.
- Also outputs the `EXRPartArtifact` and a list of `EXRChannelArtifact` descriptors. The part connects to [Display EXR Part](display_exr_part.md); [Display EXR Channel](display_exr_channel.md) and [Save EXR](save_exr.md) take individual channels, so pick each one out of `channels` with [Get From List](../../nodes/lists/get_from_list.md).
- Same color management as [Display EXR Part](display_exr_part.md).
- An **Open in external viewer** button opens the source EXR in a configured HDR viewer (see [Library settings](index.md#library-settings)).

## Typical workflow position

```text
[Read EXR] → (image output)
           ↘ part → Display EXR Part
           ↘ channels → Get From List → Display EXR Channel / Save EXR
```

## Node preview

<!-- TODO: add ../assets/read-exr.png screenshot -->

## Inputs

| Name        | Type              | Required | Notes                                                                                                                                     |
| ----------- | ----------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `path`      | `str \| Sequence` | Yes      | Path to an `.exr` file, or a frame pattern such as `render.####.exr`. A connected `Sequence` is used as-is.                               |
| `part_name` | `str`             | No       | Which part of the file to read. Hidden for single-part files.                                                                             |
| `layer`     | `str`             | No       | Which layer of the selected part to render. Hidden when the part has one layer.                                                           |
| `frame`     | `int`             | No       | Absolute frame number to read, matching the filenames on disk. Hidden for a single file. Connect **For Loop Start**'s `index` to iterate. |

## Parameters

| Name          | Type  | Default        | Notes                                                                                                                                                               |
| ------------- | ----- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `output_file` | `str` | `read_exr.png` | File name to save to, resolved into the project's output location. For a sequence, the frame number is added to the name. The saved location is carried by `image`. |

### Color Management *(collapsed by default)*

| Name                  | Type                               | Default                                                      | Notes                                                                                                                                   |
| --------------------- | ---------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `exposure`            | `float`                            | `0.0`                                                        | Exposure in EV stops applied before the display transform or OCIO transform. Range −10 to +10.                                          |
| `color_mode`          | `basic \| ocio`                    | `ocio` if the OpenColorIO Library is installed, else `basic` | `basic` uses a local display transform; `ocio` uses the connected `OCIOColorParamsArtifact`.                                            |
| `tone_mapping`        | `sRGB \| rec709 \| rec1886 \| raw` | `sRGB`                                                       | Display transform when `color_mode` is `basic`. See [OpenColorIO integration](index.md#opencolorio-integration). Hidden in `ocio` mode. |
| `color_params`        | `OCIOColorParamsArtifact`          | —                                                            | From [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md). Required when `color_mode` is `ocio`.                            |
| `embed_color_profile` | `bool`                             | `false`                                                      | Embed an ICC profile naming the display transform. Hidden in `ocio` mode.                                                               |

## Outputs

| Name       | Type                       | Notes                                                                        |
| ---------- | -------------------------- | ---------------------------------------------------------------------------- |
| `image`    | `ImageUrlArtifact`         | 8-bit PNG of the current frame, encoded with the selected display transform. |
| `part`     | `EXRPartArtifact`          | Descriptor for the part that was read.                                       |
| `channels` | `list[EXRChannelArtifact]` | One descriptor per channel in the part that was read.                        |

### EXR Info *(collapsed by default)*

| Name                           | Type  | Notes                                                    |
| ------------------------------ | ----- | -------------------------------------------------------- |
| `image_width` / `image_height` | `int` | Image dimensions in pixels.                              |
| `part_count`                   | `int` | Number of parts in the file.                             |
| `channel_count`                | `int` | Number of channels in the part that was read.            |
| `first_frame` / `last_frame`   | `int` | Lowest and highest frame numbers; `0` for a single file. |
| `frame_count`                  | `int` | Number of frames; `1` for a single file.                 |

## Tips & pitfalls

- **`part_name`, `layer`, and `frame` appear after the scan.** They stay hidden until the node has read the file and knows whether there's more than one part, layer, or frame to pick from.
- **`frame` is the number on disk, not a list position.** For `render.1001.exr`–`render.1100.exr`, frame `1001` is the first frame. Set the loop's range from the sequence's first and last frames (the `first_frame` and `last_frame` outputs show them).
- **Out-of-range frames are clamped, not failed.** A `frame` outside the sequence renders the nearest first or last frame; the result details note the clamp. A frame missing from inside the range still fails.
- **The `openexr.header_only` setting doesn't apply.** Read EXR always decodes the selected part's pixels. Use [Load EXR](load_exr.md) to inspect headers without loading pixels.
- **One part per node.** To work with several parts of a multi-part file at once, use [Load EXR](load_exr.md).

## See also

- [Load EXR](load_exr.md) — full header detail and every part at once.
- [Load EXR Sequence](load_exr_sequence.md) — per-frame descriptors for a `ForEach` loop.
- [Display EXR Part](display_exr_part.md) · [Display EXR Channel](display_exr_channel.md) — render descriptors to viewable images.
