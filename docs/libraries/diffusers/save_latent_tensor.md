# Save Latent Tensor

**Writes a `LatentArtifact` to disk as a torch `.pt` file. Useful for debugging, or building offline test fixtures.**

Category: `ModularDiffusion/IO`

## TL;DR

- Saves the raw latent tensor — not a decoded image. To round-trip back into a workflow, load the `.pt` with `torch.load(...)` and wrap it in a `LatentArtifact` (or use it directly in custom code).
- Output port (`saved_path`) is the resolved absolute path, so you can chain it into logging / metadata nodes.

## Typical workflow position

```text
Generate Media Latents → [Save Latent Tensor]
                       └→ Decode Media Latent → Save Image
```

## Node preview

<!-- TODO: add docs/assets/nodes/save-latent-tensor.png screenshot -->

## Inputs

| Name            | Type             | Required | Notes               |
| --------------- | ---------------- | -------- | ------------------- |
| `latent_tensor` | `LatentArtifact` | Yes      | The latent to save. |

## Outputs

| Name         | Type | Notes                                    |
| ------------ | ---- | ---------------------------------------- |
| `saved_path` | str  | Absolute path the tensor was written to. |

## Parameters

| Name        | Type | Default           | Notes                                                                                                       |
| ----------- | ---- | ----------------- | ----------------------------------------------------------------------------------------------------------- |
| `file_path` | str  | `debug/latent.pt` | Output path. Relative paths resolve from the workspace directory; parent folders are created automatically. |

## Tips & pitfalls

- **Tensor is detached and moved to CPU before saving** — safe to reload on any device.
- **No metadata is stored** beyond the raw tensor. If you need to round-trip the `source_shape` (used for latent unpacking), record it separately.
- **`.pt` files are large.** A 1024×1024 SDXL latent is ~4 MB; an LTX video latent can be hundreds of MB.

## See also

- [Generate Media Latents](generate_media_latents.md) — typical upstream node.
