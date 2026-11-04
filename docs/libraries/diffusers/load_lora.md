# Load LoRA

**Loads a LoRA file from local disk and exposes it as an output that the Pipeline Builder accepts.**

Category: `ModularDiffusion/Pipeline`

## TL;DR

- One LoRA per node. To stack LoRAs, drop multiple `Load LoRA` nodes and connect them all to the same consumer.
- **Two ways to use a LoRA:**
    - **Pipeline Builder** — LoRA is **fused** (baked) into the model weights at build time. `weight` is fixed at that point; changing it **rebuilds** the cached pipeline. Best for LoRAs you always want active.
    - **LoRA Pipeline** — LoRA is activated **per generation** and released afterward. The base pipeline is never modified, so changing `weight` between runs does not trigger a rebuild. Best for IC LoRAs, slider LoRAs, or workflows that swap adapters between branches.
- Accepts `.safetensors`, `.sft`, `.pt`, `.bin`, `.json`, `.lora`.

## Typical workflow position

```text
# Fused (baked into model at build time):
[Load LoRA] ─┐
[Load LoRA] ─┼─→ Pipeline Builder → Generate Media Latents
[Load LoRA] ─┘

# Per-generation (applied at run time):
Pipeline Builder ──┐
[Load LoRA] ───────┤→ LoRA Pipeline → Generate Media Latents
[Load LoRA] ───────┘
```

## Node preview

<img src="../assets/nodes/load-lora.png" alt="Load LoRA" width="480">

## Inputs

| Name        | Type            | Required | Notes                                  |
| ----------- | --------------- | -------- | -------------------------------------- |
| `file_path` | path            | Yes      | Absolute path to the LoRA file.        |
| `weight`    | float (0.0–1.0) | No       | Influence of this LoRA, default `1.0`. |

## Outputs

| Name             | Type           | Notes                                                                      |
| ---------------- | -------------- | -------------------------------------------------------------------------- |
| `loras`          | `loras` (dict) | `{path: weight}` — connect to the `loras` input on the Pipeline Builder.   |
| `trigger_phrase` | str            | Optional pass-through phrase to include in your prompt; hidden by default. |

## Tips & pitfalls

- **Hugging Face repo IDs are not supported here.** Download the file first; this node loads from disk only.
- **LoRA must match the base pipeline architecture** (Flux LoRA → Flux pipeline, etc.). Mismatches surface at pipeline-build time, not when the LoRA loads.
- **`weight` is baked in at fuse time.** Because LoRAs are fused into the model, you cannot change `weight` between generations without triggering a full pipeline rebuild.
- **Trigger phrases:** if your LoRA needs a trigger word, put it in your prompt manually — the `trigger_phrase` parameter is hidden by default and is currently passthrough metadata only.

## See also

- [Modular Diffusion Pipeline Builder](pipeline_builder.md) — fused LoRA consumer.
- [LoRA Pipeline](lora_pipeline.md) — per-generation LoRA consumer.
- Workflow template: `workflows/templates/LoRAText2Image.py`.
