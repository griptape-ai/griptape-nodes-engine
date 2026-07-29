# Multiply Latents

**Elementwise product of two latent tensors.**

Category: `ModularDiffusion/Transform`

## TL;DR

- `output = left_latent * right_latent` elementwise.
- For masked compositing of two different latents, prefer [Latents Composite Mask](latents_composite_mask.md) — it's purpose-built and handles the mask resampling for you.

## Typical workflow position

```text
Latent A ─┐
          ├─→ [Multiply Latents] → Add Latents / Generate
Latent B ─┘
```

## Node preview

<img src="../assets/nodes/multiply-latents.png" alt="Multiply Latents" width="480">

## Inputs

| Name           | Type             | Required | Notes                           |
| -------------- | ---------------- | -------- | ------------------------------- |
| `left_latent`  | `LatentArtifact` | Yes      |                                 |
| `right_latent` | `LatentArtifact` | Yes      | Must match `left_latent` shape. |

## Outputs

| Name            | Type             | Notes           |
| --------------- | ---------------- | --------------- |
| `output_latent` | `LatentArtifact` | `left * right`. |

## Tips & pitfalls

- **Elementwise, not matrix multiplication.** Each element in `a` is multiplied by the corresponding element in `b`.
- **Most useful for scaling.** Multiplying one latent by a near-constant tensor is the most predictable use; combining two arbitrary latents elementwise tends to produce unexpected results.

## See also

- [Add Latents](add_latents.md) · [Subtract Latents](subtract_latents.md) · [Latents Composite Mask](latents_composite_mask.md)
