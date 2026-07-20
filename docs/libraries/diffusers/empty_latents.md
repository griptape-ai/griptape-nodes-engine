# Create Empty Latents

**Generates a zero-filled latent tensor of the shape expected by the connected pipeline.**

Category: `ModularDiffusion/Create`

## TL;DR

- Same shape behavior as [Create Noise Latents](create_noise_latents.md), but the tensor is all zeros (no random seed).
- Useful as the **destination** for [Latents Composite Mask](latents_composite_mask.md), or as a starting point when you want fully controlled noise injection downstream.
- For ordinary text-to-image / text-to-video generation you almost always want [Create Noise Latents](create_noise_latents.md) instead.

## Typical workflow position

```text
Pipeline Builder → [Create Empty Latents] → Latents Composite Mask → Generate Media Latents
```

## Node preview

<img src="../assets/nodes/create-empty-latents.png" alt="Create Empty Latents" width="480">

## Inputs

| Name       | Type              | Required | Notes                                                      |
| ---------- | ----------------- | -------- | ---------------------------------------------------------- |
| `pipeline` | `Pipeline Config` | Yes      | Determines latent shape and whether `num_frames` is shown. |

## Outputs

| Name            | Type             | Notes                                                               |
| --------------- | ---------------- | ------------------------------------------------------------------- |
| `output_latent` | `LatentArtifact` | Zero-filled latent tensor in the pipeline's canonical latent space. |

## Parameters

| Name         | Type         | Default | Notes                                                   |
| ------------ | ------------ | ------- | ------------------------------------------------------- |
| `width`      | int (pixels) | `1024`  | Pixel-space width.                                      |
| `height`     | int (pixels) | `1024`  | Pixel-space height.                                     |
| `num_frames` | int          | `41`    | Number of video frames. **Hidden for image pipelines.** |

No `seed` parameter — output is deterministic by construction.

## Tips & pitfalls

- **Pair with noise before generating.** An all-zero latent needs noise before denoising — enable `add_noise=True` on [Generate Media Latents](generate_media_latents.md), or inject noise via composite / math nodes first. Without noise it has nothing to denoise from.

## See also

- [Create Noise Latents](create_noise_latents.md) — the usual text-to-image / text-to-video starting tensor.
- [Latents Composite Mask](latents_composite_mask.md) — primary consumer.
