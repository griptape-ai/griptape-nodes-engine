# OpenEXR Library

Professional [OpenEXR](https://openexr.com/) support for VFX and HDR pipelines.
Load EXR files and inspect their structure, display individual parts or
manually-assembled channels as tone-mapped images, and save images or channel
data back to EXR.

- **Repository**: [griptape-ai/griptape-nodes-library-openexr](https://github.com/griptape-ai/griptape-nodes-library-openexr)
- **Requirements**: none (optionally pairs with the
    [OpenColorIO Library](../opencolorio/index.md) for color-managed display)
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

| Node                                          | Description                                                                                                                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Load EXR](load_exr.md)                       | Loads an EXR file and exposes its full structure (parts, channels, header attributes) as typed outputs — header-only by default, so it's fast even on large multi-part renders |
| [Display EXR Part](display_exr_part.md)       | Renders an EXR part to an 8-bit sRGB/RGBA PNG with exposure and tone mapping (or OCIO color management)                                                                        |
| [Display EXR Channel](display_exr_channel.md) | Combines 1–4 individual EXR channels into an 8-bit display image, with optional alpha compositing over a background                                                            |
| [Save EXR](save_exr.md)                       | Saves a single-part EXR from an 8-bit image or from EXR channel artifacts, with compression, pixel type, and header metadata controls                                          |

## Quick start

```text
Load EXR → Display EXR Part → (image output)
         ↘ Display EXR Channel (assemble R/G/B/A from any parts or files)
```

1. Add a **Load EXR** node and point `file_path` at a `.exr` file. The node
    scans the header and populates dynamic **Parts** and **Channels** groups.
1. Wire a part output into **Display EXR Part** (or individual channels into
    **Display EXR Channel**) to get a tone-mapped PNG you can preview in the
    canvas or feed to other image nodes.
1. Use **Save EXR** to write images or channel data back out as EXR.

## OpenColorIO integration

The display nodes support two color modes:

- **`basic`** — local exposure + tone mapping (`filmic` or `linear`).
- **`ocio`** — a connected `OCIOColorParamsArtifact` (from the
    [OpenColorIO Library](../opencolorio/index.md)) drives an OCIO display-view
    transform.

When the OpenColorIO Library is installed, the display nodes default to `ocio`
mode automatically; otherwise they default to `basic`. The OCIO dependency is
optional — everything works without it.

## Library settings

Settings live in the engine configuration under the `openexr` category.

| Setting                     | Default | Description                                                                                                                                                         |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openexr.header_only`       | `true`  | When `true`, **Load EXR** reads only the file header (fast). Set to `false` to read accurate per-channel pixel types at the cost of loading pixel data into memory. |
| `openexr.viewer_executable` | `""`    | Full path to an external HDR viewer executable (e.g. `/usr/bin/djv`, or a Nuke binary). When empty, the OS default file association is used.                        |
| `openexr.viewer_args`       | `""`    | Additional command-line arguments passed to the viewer before the file path, e.g. `--hdr --linear`. Parsed with shell-style quoting rules.                          |

Every node has an **Open in external viewer** button that opens the source EXR
in the configured viewer (or the OS default). The viewer is launched
fire-and-forget — clicking the button never blocks the canvas.

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-openexr/issues).
