# OpenColorIO Library

Professional color management nodes built on
[OpenColorIO](https://opencolorio.org/) (OCIO) — the industry-standard color
management system used across film, VFX, and animation pipelines.

- **Repository**: [griptape-ai/griptape-nodes-library-opencolorio](https://github.com/griptape-ai/griptape-nodes-library-opencolorio)
- **Requirements**: an OCIO config file (`$OCIO` environment variable or an
    explicit path)
- **Node category**: `Colorspace` in the node picker

## Installation

In the editor, open **Manage → Library Management**, click **Add Library**, and
paste:

```text
https://github.com/griptape-ai/griptape-nodes-library-opencolorio
```

Or via the CLI:

```bash
gtn libraries download https://github.com/griptape-ai/griptape-nodes-library-opencolorio
```

See the [Libraries guide](../../guides/libraries.md) for general install,
update, and troubleshooting help.

## Prerequisites: the `$OCIO` environment variable

This library uses the `$OCIO` environment variable as the primary way to
identify your color configuration. Set it in your environment before launching
Griptape Nodes:

```bash
export OCIO=/path/to/your/config.ocio
```

Most studio workstations and render environments will have `$OCIO` set already.
If you are working with a Griptape
[project](../../guides/projects/index.md), you can set it per-project in
`project.yml`:

```yaml
environment:
  OCIO: "{project_dir}/config/aces.ocio"
```

The `$OCIO` value is re-read live at each node execution, so switching projects
automatically picks up the new config without reloading the workflow.

The library registers `OCIO` as a known environment variable, so you can also
inspect or set it from the Griptape Nodes settings UI without leaving the
application.

## Nodes

| Node                                              | Description                                                                                                                             |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| [Load OCIO Config](load_ocio_config.md)           | Loads an OCIO config from `$OCIO` or an explicit path; emits an `OCIOConfigArtifact` for downstream nodes                               |
| [OCIO Color Parameters](ocio_color_parameters.md) | Bundles source colorspace, display, and view into a reusable `OCIOColorParamsArtifact`; dropdowns populate from a connected OCIO config |

## Quick start

1. Add a **Load OCIO Config** node to your canvas.
    - If `$OCIO` is set, the node detects and displays the path automatically.
1. Wire the `config` output to an **OCIO Color Parameters** node and pick your
    source colorspace, display, and view.
1. Wire the `color_params` output to any node that accepts an
    `OCIOColorParamsArtifact` — for example the
    [Display EXR Part](../openexr/display_exr_part.md) and
    [Display EXR Channel](../openexr/display_exr_channel.md) nodes from the
    [OpenEXR Library](../openexr/index.md).

```text
Load OCIO Config → OCIO Color Parameters → (downstream transform/display nodes)
```

## Used by other libraries

Besides its picker nodes, this library provides a colorspace-transform service
that other libraries can call. The [OpenEXR Library](../openexr/index.md)
detects it automatically: when the OpenColorIO Library is installed, the EXR
display nodes default to OCIO-managed color (`color_mode: ocio`) instead of
local tone mapping.

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-opencolorio/issues).
