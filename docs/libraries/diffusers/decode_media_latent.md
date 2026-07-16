# Decode Media Latent

**Runs the pipeline's VAE decoder on a latent, producing an image or video — typically the final node in a flow.**

Category: `ModularDiffusion/Encode\Decode`

## TL;DR

- Output is **dynamic**: `output_image` for image pipelines, `output_video` (+ `fps`) for video pipelines (LTX, LTX2, WAN). It swaps automatically when you connect a `pipeline`.
- Almost always the last node in the flow. Connect to a Save Image / Save Video node downstream.

## Typical workflow position

```text
Generate Media Latents → [Decode Media Latent] → Save Image / Save Video
```

## Node preview

<img src="../assets/nodes/decode-media-latent.png" alt="Decode Media Latent" width="480">

## Inputs

| Name            | Type              | Required | Notes                                             |
| --------------- | ----------------- | -------- | ------------------------------------------------- |
| `pipeline`      | `Pipeline Config` | Yes      | Must match the pipeline that produced the latent. |
| `latent_tensor` | `LatentArtifact`  | Yes      | Latent to decode.                                 |

## Outputs

| Name           | Type               | Notes                |
| -------------- | ------------------ | -------------------- |
| `output_image` | `ImageArtifact`    | For image pipelines. |
| `output_video` | `VideoUrlArtifact` | For video pipelines. |

## Parameters

| Name  | Type        | Default | Notes                                                  |
| ----- | ----------- | ------- | ------------------------------------------------------ |
| `fps` | int (1–120) | `25`    | Output frame rate. **Only shown for video pipelines.** |

## Tips & pitfalls

- **Use the same pipeline that produced the latent.** Each pipeline carries the VAE it was trained with — decoding a latent with a mismatched VAE produces corrupt output.
- **Large latents need more VRAM to decode.** High-resolution or multi-frame latents require more memory during decode. Enable `vae_slicing` on the Pipeline Builder to decode in batches and keep peak VRAM usage lower.

## See also

- [Encode Media Latent](encode_media_latent.md) — inverse operation.
- [Generate Media Latents](generate_media_latents.md) — typical upstream node.
