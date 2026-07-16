# Diffusers Library

Build flexible media generation workflows with modular 🧨
[Diffusers](https://huggingface.co/docs/diffusers/index) pipelines.

The Diffusers Library is for creators who want more control over diffusion
execution. Instead of running a fixed, monolithic "generate image" step, you
break the diffusion process into individual, connectable stages — so you can
inspect, reuse, and customize each part of the pipeline.

!!! warning "Experimental — under active development"

    APIs, node interfaces, workflow templates, and library structure may change
    at any time without notice or migration support. Pin to a specific commit if
    you need stability, and expect breakage when updating.

- **Repository**: [griptape-ai/griptape-nodes-library-diffusers](https://github.com/griptape-ai/griptape-nodes-library-diffusers)
- **Requirements**: a GPU-capable environment (**CUDA** or **MPS**)
- **Node categories**: `ModularDiffusion/…` in the node picker

## Installation

In the editor, open **Manage → Library Management**, click **Add Library**, and
paste:

```text
https://github.com/griptape-ai/griptape-nodes-library-diffusers
```

Use the modal's **Advanced Options** to pin a branch, tag, or commit. Or via the
CLI:

```bash
gtn libraries download https://github.com/griptape-ai/griptape-nodes-library-diffusers
```

See the [Libraries guide](../../guides/libraries.md) for general install,
update, and troubleshooting help.

!!! note "Model downloads"

    All models are downloaded locally to the Hugging Face cache the first time
    they're used (default: `~/.cache/huggingface/hub` on Linux/macOS,
    `%USERPROFILE%\.cache\huggingface\hub` on Windows). To store them
    elsewhere, set the `HF_HOME` environment variable before launching the
    engine. See [Hugging Face Models](../../guides/integrations/hugging_face.md)
    for account and token setup.

## How it works

A typical flow looks like this:

1. **Build a pipeline once** with the
    [Pipeline Builder](pipeline_builder.md), and reuse it across multiple
    generations.
1. **Create or load latents** (noise, empty, encoded image/video, or a saved
    tensor).
1. **Run diffusion** with
    [Generate Media Latents](generate_media_latents.md) to produce new latents —
    optionally multiple times for multi-stage or rediffusion workflows.
1. **Transform latents** with math, masked compositing, or upsampling between
    stages.
1. **Decode** the final latents back into images or video with
    [Decode Media Latent](decode_media_latent.md).

Because every stage is a node, you can branch, chain, and reorder steps —
enabling patterns like multi-stage refinement, ControlNet stacking, latent
composition, first/last-frame video conditioning, and latent upscaling that
aren't possible with a single end-to-end generate node.

## Supported models

Models are selected on the Pipeline Builder via a `provider` dropdown.
Currently supported:

- **Flux** and **Flux2** (including Flux2-Klein)
- **Stable Diffusion XL**
- **Qwen-Image** (and Qwen-Edit)
- **Z-Image**
- **LTX** (video)
- **LTX-2.x** (text/image/video-to-video, with image and video conditioning,
    IC-LoRA, and HDR IC-LoRA for linear HDR output)
- **WAN** (text-to-video and image-to-video)

Models are loaded from Hugging Face repositories in Diffusers format
(single-file `.safetensors` checkpoints are not loaded directly — use a Hugging
Face repo ID). Multiple **LoRAs** can be attached to a pipeline via the
builder.

## Node reference

Node groups mirror the categories in the node picker.

### Pipeline

- [Modular Diffusion Pipeline Builder](pipeline_builder.md)
- [ControlNet Pipeline](controlnet_pipeline.md)
- [Load LoRA](load_lora.md)
- [LoRA Pipeline](lora_pipeline.md)

### Create

- [Create Noise Latents](create_noise_latents.md)
- [Create Empty Latents](empty_latents.md)

### Processing

- [Generate Media Latents](generate_media_latents.md)
- [Latent Upsampler](latent_upsampler.md)

### Transform

- [Add Latents](add_latents.md)
- [Subtract Latents](subtract_latents.md)
- [Multiply Latents](multiply_latents.md)
- [Latents Composite Mask](latents_composite_mask.md)

### Conditioning

- [Configure ControlNet](configure_controlnet.md)
- [Media Generation Conditioning](media_gen_conditioning.md)

### Encode / Decode

- [Encode Media Latent](encode_media_latent.md)
- [Encode Masked Media Latent](encode_masked_media_latent.md)
- [Decode Media Latent](decode_media_latent.md)
- [Decode HDR Latents](decode_hdr_latents.md)

### IO

- [Save Latent Tensor](save_latent_tensor.md)

## Live previews

Enable live image previews to stream intermediate decoded images during
generation. Useful for monitoring long runs, at the cost of inference speed.

1. Open **Settings → Library Settings** in the editor.
1. Scroll to the **Modular Diffusion Library** section.
1. Toggle **Enable Image Preview Intermediates** on.

## Performance and memory

The Pipeline Builder caches the loaded pipeline in memory and reuses it across
runs, only rebuilding when the configuration changes.

For lower-VRAM setups, the builder exposes a **Memory Optimization Strategy**
selector:

- **Automatic** — Griptape picks reasonable defaults for the chosen model.
- **Manual** — you control each knob individually:
    - **Quantization mode**: `fp8`, `int8`, or `int4` (via `optimum-quanto` /
        `bitsandbytes`) — shrinks transformer weights at the cost of some
        quality.
    - **CPU offload strategy**: `Model` (whole submodules) or `Sequential`
        (per-layer) — moves weights to CPU when idle to free VRAM, at the cost
        of inference speed.
    - **Attention slicing** — runs attention in smaller chunks; cheap memory
        win, small speed hit.
    - **VAE slicing** — decodes the latent in batches of 1; helps with large
        batch sizes.
    - **Transformer layerwise casting** — keeps the transformer in a lower
        precision and upcasts per layer during compute.

Enable only what you need — each option trades some speed for memory. The
Pipeline Builder includes per-parameter help badges with more detail.

!!! warning "Using alongside the Advanced Media Library"

    The Diffusers Library and the
    [Advanced Media Library](../../nodes/advanced_media_library/diffusion_pipelines.md)
    share several upstream dependencies (e.g. `diffusers`, `transformers`,
    `torch`) and each maintains its own in-memory pipeline cache. If both
    libraries are loaded and used in the same session, you may hold two full
    copies of overlapping model weights in VRAM/RAM at once, leading to
    out-of-memory errors. Prefer using one library per session, or reduce
    memory pressure via the optimization knobs above.

## Workflow templates included

- Text2Image
- MultistageText2Image
- LoRAText2Image
- ControlnetText2Image
- Image2Image
- FirstAndLastFrameImage2Video
- LTX23-HDR-Text2Video-Upsample-Two-Stage

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-diffusers/issues).
