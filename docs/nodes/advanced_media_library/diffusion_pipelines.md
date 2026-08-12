# Diffusion Pipelines

!!! warning "You need to perform setup steps to use Hugging Face Diffusion Pipeline nodes"

    [This guide](../../guides/integrations/hugging_face.md) will walk you through setting up a Hugging Face account, creating an access token, and installing the required models to make this node fully functional.

## What are they?

The Diffusion Pipeline system consists of two complementary nodes that work together to provide efficient image generation:

- **Diffusion Pipeline Builder**: Builds and caches 🤗 Diffusers Pipelines for reuse across multiple execution nodes
- **Generate Image (Diffusion Pipeline)**: Generates images using the cached pipelines

This modular approach allows you to configure a pipeline once and reuse it multiple times, improving performance and resource efficiency. The system supports a wide range of providers and models through dynamic parameters.

## Supported Providers

The Diffusion Pipeline Builder supports multiple AI model providers:

- **Flux** - High-quality text-to-image generation
- **Qwen** - Multimodal capabilities
- **Stable Diffusion** - Popular open-source diffusion models
- **Allegro** - Video generation capabilities
- **Amused** - Efficient masked image modeling
- **AudioLDM** - Audio generation from text
- **WAN** - Specialized image generation
- **Wuerstchen** - Efficient diffusion architecture
- **Custom** - Support for custom pipeline configurations and self-provided models

## When would I use it?

Use these nodes when you need to:

- Generate images from textual descriptions with various model architectures
- Leverage advanced image generation models for creative projects
- Experiment with different providers and model configurations
- Optimize performance by reusing cached pipelines across multiple generations
- Work with specialized models for audio, video, or multimodal generation

## How to use it

### Basic Setup

The Diffusion Pipeline system uses a two-node workflow:

1. **Configure the Builder**:

    - Add a "Diffusion Pipeline Builder" node to your workflow
    - Select your desired provider (Flux, Stable Diffusion, etc.)
    - Configure provider-specific parameters (model, LoRAs, optimizations)
    - Run the builder to cache the pipeline

1. **Generate Images**:

    - Add a "Generate Image (Diffusion Pipeline)" node
    - Connect the pipeline output from the builder to the runtime node
    - Configure generation parameters (prompt, dimensions, steps, etc.)
    - Run the runtime node to generate images

### Pipeline Builder Parameters

The builder node has dynamic parameters that change based on the selected provider:

- **provider**: Select from supported providers (Flux, Stable Diffusion, etc.)
- **Provider-specific parameters**: Model selection, LoRA configurations, optimization settings
- **pipeline**: Output connection containing the cached pipeline configuration

### Runtime Parameters

The runtime node parameters are dynamically generated based on the connected pipeline:

- **pipeline**: Input connection from the builder node
- **Dynamic generation parameters**: Prompts, dimensions, inference steps, guidance scales
- **output_image**: The generated image as an ImageArtifact
- **seed**: An integer seed for random number generation
- **logs**: Detailed logs of the generation process

!!! note "Dynamic Parameters"

    Both nodes use dynamic parameters that automatically adjust based on your selections. The available parameters will change when you select different providers or connect different pipelines.

### Advanced Features

- **Pipeline Caching**: Built pipelines are cached using configuration hashes for efficient reuse
- **LoRA Support**: Load and configure LoRA adapters for model customization
- **Optimization Options**: Enable various optimizations for better performance
- **Real-time Previews**: Optional intermediate image previews during generation (may slow inference)
- **Connection Preservation**: Runtime node preserves parameter connections when pipeline changes

## Manual Memory Settings

The Diffusion Pipeline Builder ships with `memory_optimization_strategy` set to **Manual** by default. Manual mode is the default because the Automatic strategy is conservative — it enables every optimization needed to fit the model, which bottlenecks powerful GPUs and slows generation on capable hardware. The tradeoff is that manual mode exposes a row of toggles that assume some familiarity with [🤗 Diffusers memory optimization concepts](https://huggingface.co/docs/diffusers/main/en/optimization/memory).

This section explains each manual-mode parameter: what it does, the tradeoff it makes, and a heuristic for when to enable it.

### `attention_slicing`

- **What it does**: Computes the attention operation in sequential slices instead of all at once, lowering peak VRAM during attention.
- **Tradeoff**: Lower memory at the cost of speed (often 5–20% slower).
- **When to enable**: When you hit out-of-memory errors during generation, particularly on GPUs with less than 8 GB VRAM, on Apple Silicon (MPS) with less than 64 GB unified memory, or on CPU. Leave off when you have headroom.

### `vae_slicing`

- **What it does**: Decodes the VAE latent in batched slices rather than as a single tensor.
- **Tradeoff**: Lower peak memory during the VAE decode step, with negligible speed cost.
- **When to enable**: When generating multiple images in a single batch, or when decoding at higher resolutions exhausts VRAM in the final step. Cheap to leave on; effectively free for batch sizes of 1.

### `transformer_layerwise_casting`

- **What it does**: Stores the transformer (or UNet) weights in fp8 (`float8_e4m3fn`) and upcasts each layer to bfloat16 only while it is computing.
- **Tradeoff**: Roughly halves transformer weight memory vs. bfloat16, with a small speed cost from per-layer casting and a small quality hit on some models.
- **When to enable**: When the model fits in VRAM only after weight compression, but you don't want a full quantization pass. Skip if the pipeline is pre-quantized or doesn't support layerwise casting — the node logs a notice and ignores the toggle in those cases.

### `cpu_offload_strategy`

- **What it does**: Moves pipeline components between CPU RAM and GPU VRAM to reduce GPU memory residency.
- **Choices**:
    - **None** — All components stay on the GPU. Fastest, requires the most VRAM.
    - **Model** — One full submodel (e.g. text encoder, transformer, VAE) is on the GPU at a time; others live in CPU RAM and swap in as needed. Moderate VRAM savings, modest speed penalty.
    - **Sequential** — Even finer-grained: individual `nn.Module` layers are streamed to GPU on demand. Largest VRAM savings, largest speed penalty (often several times slower).
- **When to use which**:
    - **None** if the pipeline already fits with room to spare.
    - **Model** if you're a few GB short of fitting everything resident.
    - **Sequential** as a last resort to run a model that wouldn't otherwise load.

### `quantization_mode`

- **What it does**: Quantizes pipeline weights via `optimum-quanto` to `fp8`, `int8`, or `int4` before inference.
- **Tradeoff**: Significant memory savings (`int4` ≈ 1/4 the size of bfloat16) at increasing risk of quality loss as the bit width drops. The first run also pays a one-time quantization cost.
- **When to enable**: When even with offloading the model won't fit, or when you want to free VRAM for other work (larger batch, longer context, additional LoRAs). `fp8` is usually a safe starting point; drop to `int8`/`int4` only if needed.

### If you run out of memory

Escalate one knob at a time in this order: enable `vae_slicing` → enable `attention_slicing` → switch `cpu_offload_strategy` to `Model` → enable `transformer_layerwise_casting` → step `quantization_mode` down (`fp8` → `int8` → `int4`) → switch `cpu_offload_strategy` to `Sequential`.

### When in doubt: switch to Automatic

Automatic mode runs the pipeline through a memory-aware decision tree (see `_automatic_optimize_diffusion_pipeline` in `pipeline_utils.py`) that enables only the optimizations needed to fit the model on the detected device. It's slower than a hand-tuned manual configuration on capable hardware, but it's a safe fallback when you don't want to hand-pick settings.

For a deeper treatment of the underlying concepts, see the upstream [🤗 Diffusers memory optimization guide](https://huggingface.co/docs/diffusers/main/en/optimization/memory).

## Performance Optimization

- **Reuse Pipelines**: Build once, generate many times by connecting multiple runtime nodes to one builder
- **Cache Management**: Pipelines are automatically cached and reused across workflow runs
- **Memory Management**: Configure optimization settings in the builder for your hardware
- **Preview Settings**: Disable intermediate previews for faster generation

## Common Issues

- **Missing API Key**: Ensure the Hugging Face API token is set as `HUGGINGFACE_HUB_ACCESS_TOKEN`; instructions for that are in [this guide](../../guides/integrations/hugging_face.md)
- **Pipeline Not Found**: If you see cache errors, ensure the builder node has been executed successfully
- **Memory Constraints**: Large models or high-resolution generation may require significant GPU memory
- **Provider Compatibility**: Ensure your selected model is compatible with the chosen pipeline type
