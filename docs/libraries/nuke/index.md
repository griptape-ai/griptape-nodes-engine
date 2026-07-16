# Nuke Library

Build workflows that run inside [Foundry Nuke](https://www.foundry.com/products/nuke-family).
The library provides flow-control nodes for authoring Nuke-targeted workflows,
a publisher that packages a workflow as a versioned `.gizmo` you can drop into
a Nuke script, and a **Nuke Script** node that runs an existing `.nk` script
headlessly and surfaces its annotated nodes as typed ports on the Griptape
canvas.

- **Repository**: [griptape-ai/griptape-nodes-library-nuke](https://github.com/griptape-ai/griptape-nodes-library-nuke)
- **Requirements**: a local Foundry Nuke installation and license
- **Node category**: `Foundry Nuke` in the node picker

## Installation

In the editor, open **Manage → Library Management**, click **Add Library**, and
paste:

```text
https://github.com/griptape-ai/griptape-nodes-library-nuke
```

Or via the CLI:

```bash
gtn libraries download https://github.com/griptape-ai/griptape-nodes-library-nuke
```

See the [Libraries guide](../../guides/libraries.md) for general install,
update, and troubleshooting help.

## Nodes

| Node                                  | Description                                                                                                    |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| [Nuke Start Flow](nuke_start_flow.md) | Entry point for a Nuke-targeted workflow — required to publish the workflow as a gizmo                         |
| [Nuke End Flow](nuke_end_flow.md)     | Terminal node for a Nuke-targeted workflow; exposes `was_successful` and `result_details`                      |
| [Nuke Script](nuke_script.md)         | Runs a `.nk` script headlessly via `nuke -t`, surfacing annotated Read/Write nodes as typed input/output ports |

## Configuring Nuke installations

### Auto-discovery

The library scans standard install locations on startup and populates the
**Nuke Version** dropdown automatically:

| OS      | Locations scanned                                   |
| ------- | --------------------------------------------------- |
| macOS   | `/Applications/Nuke*`                               |
| Windows | `%ProgramFiles%\Nuke*`, `%ProgramFiles(x86)%\Nuke*` |
| Linux   | `/usr/local/Nuke*`, `/opt/Nuke*`, `~/Nuke*`         |

It also checks `PATH` for executables named `Nuke` or `nuke`. Click
**Refresh UI** on the node to re-run discovery after installing a new version.

### Manual configuration via Engine Settings

For installs in non-standard locations, add entries to the
`nuke.installations` key in Engine Settings:

```json
{
  "nuke": {
    "installations": [
      {
        "display_name": "Nuke 16.0v7",
        "executable_path": "/opt/nuke/16.0v7/Nuke16.0",
        "annotator_nuke_version": 16
      }
    ]
  }
}
```

| Field                    | Required | Description                                                                        |
| ------------------------ | -------- | ---------------------------------------------------------------------------------- |
| `display_name`           | yes      | Label shown in the **Nuke Version** dropdown                                       |
| `executable_path`        | yes      | Absolute path to the Nuke binary                                                   |
| `annotator_nuke_version` | no       | Nuke major version; controls which Annotator panel build is loaded (default: `16`) |
| `env_overrides`          | no       | Extra environment variables merged into the Nuke subprocess                        |
| `notes`                  | no       | Free-text notes; not used at runtime                                               |

### Additional engine settings

| Key               | Description                                                            |
| ----------------- | ---------------------------------------------------------------------- |
| `nuke.executable` | Fallback Nuke binary path used when no installation entry is selected  |
| `nuke.env`        | Global environment variables injected into every Nuke subprocess       |
| `nuke.nuke_path`  | List of extra directories appended to `NUKE_PATH` for every invocation |

### Foundry license

Set `foundry_LICENSE` in the Griptape Secrets panel. It is injected into the
Nuke subprocess automatically — do not hardcode it in `env_overrides`.

## Publishing a workflow as a Nuke gizmo

1. Build a workflow whose top-level flow starts with a
    [Nuke Start Flow](nuke_start_flow.md) node and ends with a
    [Nuke End Flow](nuke_end_flow.md) node.
1. Trigger **Publish Workflow** (see
    [Publishing Workflows](../../guides/publishing.md)). In the dialog, pick:
    - A **Nuke install** (auto-detected) to resolve plugin path candidates.
    - A **gizmo install path** — either `~/.nuke`, a path from `NUKE_PATH`, a
        Nuke-install plugins directory, or a custom path.
    - An **update mode** to pick between creating a new version and
        overwriting the current one.
1. Inside Nuke, use the `Griptape` menu on the Nodes toolbar to create the
    gizmo, or run `Griptape > Refresh Griptape Gizmos` from the main menu bar
    after publishing to pick up new versions without restarting Nuke.

Publishing writes a versioned `.gizmo` plus a runner script under the chosen
install directory, and a `menu.py` that adds a `Griptape` submenu to Nuke's
Nodes toolbar. Multiple published versions of the same workflow are grouped
under a per-workflow submenu. Workflow outputs land next to the `.nk` file
(under `griptape_outputs/<workflow_name>/...`).

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-nuke/issues).
