# OCIO Color Parameters

**Bundles source colorspace, display, and view into a single reusable `OCIOColorParamsArtifact` for downstream color-managed nodes.**

Category: `Colorspace`

## TL;DR

- Wire a `config` from [Load OCIO Config](load_ocio_config.md) and the three dropdowns populate from that config.
- **Source Colorspace** lists role aliases first (e.g. `scene_linear`, `compositing_log`) so stable, pipeline-safe names appear at the top.
- Output is an `OCIOColorParamsArtifact` — one node can drive multiple downstream consumers to keep the selection in one place.
- Selections are re-validated against the live config on every run; mismatches produce an inline warning but do not fail execution.

## Typical workflow position

```text
Load OCIO Config → [OCIO Color Parameters] → (downstream transform/display nodes)
```

## Node preview

<!-- TODO: add ../assets/ocio-color-parameters.png screenshot -->

## Inputs

| Name                | Type                 | Required | Notes                                                                                                                                                                |
| ------------------- | -------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config`            | `OCIOConfigArtifact` | No       | From [Load OCIO Config](load_ocio_config.md). Enables dropdown population and validation. Without it, dropdowns show a placeholder and the node emits empty strings. |
| `source_colorspace` | `str` (choice)       | No       | Source colorspace name from the connected config. Roles listed first, then all colorspace names.                                                                     |
| `display`           | `str` (choice)       | No       | Display device name from the connected config.                                                                                                                       |
| `view`              | `str` (choice)       | No       | View name for the selected display. Choices update automatically when `display` changes.                                                                             |

## Outputs

| Name             | Type                      | Notes                                                 |
| ---------------- | ------------------------- | ----------------------------------------------------- |
| `color_params`   | `OCIOColorParamsArtifact` | Carries `source_colorspace`, `display`, and `view`.   |
| `was_successful` | `bool`                    | Whether the bundle was emitted cleanly.               |
| `result_details` | `str`                     | Validation and emit details (collapsed status group). |

## Tips & pitfalls

- **Prefer role names for source colorspace.** Roles like `scene_linear` survive config changes; raw colorspace names may not exist in the next config revision.
- **Warnings don't block execution.** If a selected value is no longer present in the live config (config changed, or a value was wired in via INPUT), the node emits the artifact as-is and flags the mismatch with an inline warning — check it before trusting downstream renders.
- **Reuse one node across consumers.** Wire `color_params` to every downstream transform/display node instead of duplicating dropdown selections per node.
- **No config connected is fine for scaffolding.** You can lay out the workflow before a config is available; the node emits empty strings until one is connected.

## See also

- [Load OCIO Config](load_ocio_config.md) — provides the `config` input.
