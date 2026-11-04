# Add Latents

**Elementwise sum of two latent tensors.**

Category: `ModularDiffusion/Transform`

## TL;DR

- `output = left_latent + right_latent` elementwise.
- Both inputs must share the same shape and latent space (i.e. come from the same pipeline type).
- Common uses: blending two denoised latents, injecting controlled noise, residual additions between stages.

## Typical workflow position

```text
Generate Media Latents (A) ─┐
                            ├─→ [Add Latents] → Generate / Decode
Generate Media Latents (B) ─┘
```

## Node preview

<img src="../assets/nodes/add-latents.png" alt="Add Latents" width="480">

## Inputs

| Name           | Type             | Required | Notes                           |
| -------------- | ---------------- | -------- | ------------------------------- |
| `left_latent`  | `LatentArtifact` | Yes      |                                 |
| `right_latent` | `LatentArtifact` | Yes      | Must match `left_latent` shape. |

## Outputs

| Name            | Type             | Notes           |
| --------------- | ---------------- | --------------- |
| `output_latent` | `LatentArtifact` | `left + right`. |

## Tips & pitfalls

- **Both inputs must have the same shape.** Resize or upsample one of the inputs first if they differ.
- **Keep latents within the same pipeline family.** Each model uses a different latent space — mixing latents across families (e.g. Flux and SDXL) produces meaningless results.

## See also

- [Subtract Latents](subtract_latents.md) · [Multiply Latents](multiply_latents.md) · [Latents Composite Mask](latents_composite_mask.md)
