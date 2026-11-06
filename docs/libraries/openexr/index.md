# OpenEXR Library

Professional [OpenEXR](https://openexr.com/) support for VFX and HDR pipelines.
Read EXR files and frame sequences, inspect their structure, display individual
parts or manually-assembled channels as viewable images, encode sequences to
preview videos, and save images or channel data back to EXR.

- **Repository**: [griptape-ai/griptape-nodes-library-openexr](https://github.com/griptape-ai/griptape-nodes-library-openexr)
- **Requirements**: Griptape Nodes engine 0.104.0 or later (optionally pairs
    with the [OpenColorIO Library](../opencolorio/index.md) for color-managed
    display)
- **Node category**: `EXR` in the node picker

## Installation

In the editor, open **Manage → Library Management**, click **Add Library**, and
paste:

```text
https://github.com/griptape-ai/griptape-nodes-library-openexr
```

Or via the CLI:

```bash
gtn libraries download https://github.com/griptape-ai/griptape-nodes-library-openexr
```

See the [Libraries guide](../../guides/libraries.md) for general install,
update, and troubleshooting help.

## Nodes

| Node                                                  | Description                                                                                                                                                                    |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Read EXR](read_exr.md)                               | Reads an EXR file or one frame of a sequence and outputs a viewable image plus the part and channel descriptors, in a single node                                              |
| [Load EXR](load_exr.md)                               | Loads an EXR file and exposes its full structure (parts, channels, header attributes) as typed outputs — header-only by default, so it's fast even on large multi-part renders |
| [Load EXR Sequence](load_exr_sequence.md)             | Scans every frame header of an EXR sequence and outputs an ordered list of part descriptors, ready for a `ForEach` loop                                                        |
| [Display EXR Part](display_exr_part.md)               | Renders an EXR part to an 8-bit RGB/RGBA PNG with exposure and a display transform (or OCIO color management)                                                                  |
| [Display EXR Channel](display_exr_channel.md)         | Combines 1–4 individual EXR channels into an 8-bit display image, with optional alpha compositing over a background                                                            |
| [Save EXR](save_exr.md)                               | Saves a single-part EXR from an 8-bit image or from EXR channel artifacts, with compression, pixel type, and header metadata controls                                          |
| [EXR Sequence to Preview](exr_sequence_to_preview.md) | Encodes an EXR frame sequence to an MP4 or MOV preview video with exposure and a display transform (or OCIO color management)                                                  |

## Quick start

```text
Read EXR → (image output)
         ↘ part / channels → Display EXR Channel / Save EXR
```

1. Add a **Read EXR** node and point `path` at a `.exr` file or a frame pattern
    such as `render.####.exr`. It outputs a viewable PNG of the selected part,
    layer, and frame.
1. For full header detail or several parts at once, use **Load EXR** and wire
    its part outputs into **Display EXR Part** (or individual channels into
    **Display EXR Channel**).
1. Use **Save EXR** to write images or channel data back out as EXR.

For frame sequences, **Load EXR Sequence** feeds a `ForEach` loop for per-frame
work, and **EXR Sequence to Preview** encodes the whole sequence to a video.

EXR files also get an automatically generated preview thumbnail wherever the
editor shows artifact previews, such as the file browser.

## OpenColorIO integration

Read EXR, Display EXR Part, Display EXR Channel, and EXR Sequence to Preview
support two color modes:

- **`basic`** — exposure plus a local display transform, chosen with
    `tone_mapping`:
    - `sRGB` (default) — the sRGB transfer function.
    - `rec709` — the BT.709 OETF.
    - `rec1886` — BT.1886 (gamma 2.4).
    - `raw` — clamps to [0, 1] with no transfer function.
- **`ocio`** — a connected `OCIOColorParamsArtifact` (from the
    [OpenColorIO Library](../opencolorio/index.md)) drives an OCIO display-view
    transform.

The `basic` transforms match the views in Nuke's default config and, like that
viewer, clip scene values above 1.0. Use `ocio` mode for a tone-mapped view.

When the OpenColorIO Library is installed, these nodes default to `ocio` mode
automatically; otherwise they default to `basic`. The OCIO dependency is
optional — everything works without it.

### Embedding a color profile

Without a profile, viewers read every PNG as sRGB, so `rec709` and `rec1886`
look different on screen from what they encode. Turn on `embed_color_profile`
(off by default, `basic` mode only) on Read EXR, Display EXR Part, or Display
EXR Channel to write an ICC profile naming the transform, so color-managed
applications such as Photoshop or a browser display it correctly. EXR Sequence
to Preview has no such switch: a video can't carry an ICC profile, so in
`basic` mode it tags the video's color transfer characteristics instead.

### Workflows saved with `filmic` or `linear`

Library v0.3.0 replaced the `filmic` and `linear` choices. A workflow that
still uses one in `basic` mode fails with a message naming a replacement:
`sRGB`, `raw` for unencoded linear output, or `ocio` mode for a real
tone-mapped view transform.

## Library settings

Settings live in the engine configuration under the `openexr` category.

| Setting                     | Default | Description                                                                                                                                                                                                   |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openexr.header_only`       | `true`  | When `true`, **Load EXR** reads only the file header (fast). Set to `false` to read accurate per-channel pixel types at the cost of loading pixel data into memory. Read EXR and Load EXR Sequence ignore it. |
| `openexr.viewer_executable` | `""`    | Full path to an external HDR viewer executable (e.g. `/usr/bin/djv`, or a Nuke binary). When empty, the OS default file association is used.                                                                  |
| `openexr.viewer_args`       | `""`    | Additional command-line arguments passed to the viewer before the file path, e.g. `--hdr --linear`. Parsed with shell-style quoting rules.                                                                    |

Read EXR, Load EXR, Display EXR Part, and Display EXR Channel have an **Open in
external viewer** button that opens the source EXR in the configured viewer (or
the OS default). The viewer is launched
fire-and-forget — clicking the button never blocks the canvas.

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-openexr/issues).
