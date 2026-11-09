# EXR Sequence to Preview

**Encodes an EXR frame sequence to an MP4 or MOV preview video, with exposure and a display transform or OCIO color management.**

Category: `EXR`

## TL;DR

- Takes a `Sequence` (from **Scan Sequence**), converts every frame to 8-bit, and encodes the result with ffmpeg.
- Same color modes as [Display EXR Part](display_exr_part.md): a `basic` display transform or `ocio` via a connected `OCIOColorParamsArtifact`.
- In `basic` mode, tags the video's color transfer characteristics to match the display transform. `ocio` output is left untagged. There's no `embed_color_profile` switch, since a video can't carry an ICC profile.
- Frame conversion runs in parallel across a configurable number of workers.

## Typical workflow position

```text
Scan Sequence → [EXR Sequence to Preview] → (video output)
```

## Node preview

<!-- TODO: add ../assets/exr-sequence-to-preview.png screenshot -->

## Inputs

| Name           | Type                      | Required | Notes                                                                                                        |
| -------------- | ------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `sequence`     | `Sequence`                | Yes      | EXR frame sequence from **Scan Sequence**.                                                                   |
| `color_params` | `OCIOColorParamsArtifact` | No       | From [OCIO Color Parameters](../opencolorio/ocio_color_parameters.md). Required when `color_mode` is `ocio`. |

## Parameters

| Name               | Type                               | Default                                                      | Notes                                                                                                                                   |
| ------------------ | ---------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `frame_rate`       | `float`                            | `24.0`                                                       | Output frame rate in fps.                                                                                                               |
| `part_index`       | `int`                              | `0`                                                          | Zero-based EXR part index to read from every frame.                                                                                     |
| `exposure`         | `float`                            | `0.0`                                                        | Exposure in EV stops applied before the display transform or OCIO transform. Range −10 to +10.                                          |
| `color_mode`       | `basic \| ocio`                    | `ocio` if the OpenColorIO Library is installed, else `basic` | `basic` uses a local display transform; `ocio` uses the connected `OCIOColorParamsArtifact`.                                            |
| `tone_mapping`     | `sRGB \| rec709 \| rec1886 \| raw` | `sRGB`                                                       | Display transform when `color_mode` is `basic`. See [OpenColorIO integration](index.md#opencolorio-integration). Hidden in `ocio` mode. |
| `format`           | `mp4 \| mov`                       | `mp4`                                                        | Output container.                                                                                                                       |
| `processing_speed` | `fast \| balanced \| quality`      | `balanced`                                                   | Encoding speed vs. quality trade-off.                                                                                                   |
| `output_file`      | `str`                              | `preview.mp4`                                                | File name to save to, resolved into the project's output location. The saved location is carried by `video`.                            |

### Advanced *(collapsed by default)*

| Name      | Type  | Default | Notes                                                 |
| --------- | ----- | ------- | ----------------------------------------------------- |
| `threads` | `int` | `4`     | Number of concurrent frame-conversion workers (1–16). |

## Outputs

| Name               | Type               | Notes                                                                         |
| ------------------ | ------------------ | ----------------------------------------------------------------------------- |
| `video`            | `VideoUrlArtifact` | The encoded preview video.                                                    |
| `frame_count`      | `int`              | Total number of frames in the sequence.                                       |
| `frames_processed` | `int`              | Frames finished so far, including failed conversions, updated during the run. |

## Tips & pitfalls

- **If every frame fails to convert,** the node reports the per-frame error rather than an ffmpeg error.
- **Rename `output_file` when you pick `mov`.** The file name defaults to `preview.mp4` and isn't changed to match `format`.
- **`rec1886` is tagged as BT.709.** No video tag exists for gamma 2.4, so players treat it as BT.709.
- **`fast` trades quality for speed.** Use it for quick checks and `quality` for previews you'll share.

## See also

- [Load EXR Sequence](load_exr_sequence.md) — per-frame descriptors for custom per-frame work.
- [Read EXR](read_exr.md) — inspect a single frame of the sequence.
