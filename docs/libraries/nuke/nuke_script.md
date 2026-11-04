# Nuke Script

**Runs an existing `.nk` script headlessly via `nuke -t`, surfacing annotated Read/Write nodes as typed input/output ports on the Griptape canvas.**

Category: `Foundry Nuke`

## TL;DR

- Point `script_path` at a `.nk` file, annotate its I/O nodes with the Griptape Annotator panel inside Nuke, and the node grows typed ports matching those annotations.
- Inputs accept images, video, or raw file paths; outputs can be images, image sequences, video, or 3D geometry.
- **Open in Nuke** launches the script in the Nuke GUI with the Annotator panel loaded; **Refresh UI** re-reads annotations and re-runs installation discovery.
- Annotations live in a `<script>.gt.json` sidecar next to the `.nk` — the node warns when the sidecar looks stale.

## Typical workflow position

```text
(image/video source) → [Nuke Script] → (image/video/3D consumers)
```

## Node preview

<!-- TODO: add ../assets/nuke-script.png screenshot -->

## Quick start

1. Drop a **Nuke Script** node onto the canvas.
1. Set **Script Path** to your `.nk` file.
1. Click **Open in Nuke** — this launches Nuke with the Griptape Annotator
    panel loaded and any current knob overrides pre-applied.
1. In Nuke, open **Panels > Griptape Annotator**. On the **Annotate** tab, mark
    Read nodes as inputs and Write nodes as outputs, optionally expose knobs
    from other nodes, then click **Save Annotations**.
1. Back in Griptape, click **Refresh UI** (or re-select the script path). The
    node grows typed input/output ports matching your annotations.
1. Wire inputs, set **Frame Start** / **Frame End**, and run the node.

## Inputs

| Name                     | Type                                                                           | Required | Notes                                                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------ | -------- | ----------------------------------------------------------------------------------------------------------------------- |
| `script_path`            | `str`                                                                          | Yes      | Path to the `.nk` file.                                                                                                 |
| `<annotated Read nodes>` | `str \| ImageArtifact \| ImageUrlArtifact \| VideoUrlArtifact \| BlobArtifact` | No       | One port per Read node annotated as an input. URL-based artifacts are downloaded to a temporary file before the render. |

## Outputs

Write nodes annotated as outputs surface in the **Outputs** group after the
node runs. The artifact type depends on the annotation:

| Annotation type                         | Griptape output                                               |
| --------------------------------------- | ------------------------------------------------------------- |
| `ImageArtifact` / `ImageUrlArtifact`    | `ImageUrlArtifact` (single rendered frame)                    |
| `VideoUrlArtifact`                      | `VideoUrlArtifact` (rendered video file, e.g. `.mp4`)         |
| `ImageSequenceArtifact`                 | `ListArtifact` of `ImageUrlArtifact` — one per rendered frame |
| `ThreeDUrlArtifact` / `GLTFUrlArtifact` | `ThreeDUrlArtifact` (`.obj` or `.glb`)                        |

## Parameters

| Name                | Type   | Default        | Notes                                                                                                                                                                                     |
| ------------------- | ------ | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `nuke_installation` | choice | auto-detected  | **Nuke Version** dropdown, populated by installation discovery (see the [library overview](index.md#configuring-nuke-installations)).                                                     |
| `frame_start`       | `int`  | script default | First frame to render.                                                                                                                                                                    |
| `frame_end`         | `int`  | script default | Last frame to render.                                                                                                                                                                     |
| `<exposed knobs>`   | varies | script value   | Knobs promoted via the Annotator panel's **Expose** tab, grouped under their node name. Leave a field empty to use the value already in the script; set it to override before the render. |

### Advanced *(collapsed by default)*

| Name                | Type   | Default | Notes                                                                                                                           |
| ------------------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `nuke_executable`   | `str`  | `""`    | Absolute path to a Nuke binary; overrides the Engine Settings selection for this node only.                                     |
| `baked_script_path` | `str`  | `""`    | Destination for a baked copy of the `.nk`.                                                                                      |
| `save_baked_copy`   | button | —       | Writes a copy of the script with all current parameter values baked into the knobs — useful for archiving or manual inspection. |

## Tips & pitfalls

- **Re-save annotations after editing the script.** If the `.nk` is saved after annotation, the node warns that the `<script>.gt.json` sidecar may be stale — re-open in Nuke and re-save annotations to refresh it.
- **Ports only appear after annotation.** A freshly-pointed script has no I/O ports; run through the Annotator panel first, then click **Refresh UI**.
- **Set the license before running.** Configure `foundry_LICENSE` in the Griptape Secrets panel; headless renders fail without a reachable license.
- **Use the version dropdown, not `PATH` luck.** Pin the exact Nuke build with `nuke_installation` (or the per-node `nuke_executable` override) so renders are reproducible across machines.

## See also

- [Nuke Start Flow](nuke_start_flow.md) · [Nuke End Flow](nuke_end_flow.md) — wrap the flow for gizmo publishing.
