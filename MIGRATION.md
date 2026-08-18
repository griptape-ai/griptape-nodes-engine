# Unreleased

## Agent streaming payloads carry `thread_id`

`AgentStreamEvent`, `AgentThinkingEvent`, `AgentToolCallEvent`, and `AgentToolResultEvent`
now require a `thread_id` naming the conversation they belong to. Execution events are
broadcast to every connected client, so a client with more than one chat surface open
previously had to assume a delta belonged to whichever turn it started last. The id
matches the `thread_id` on the `RunAgentResultSuccess` that ends the turn.

If your code constructs one of these payloads, add the thread id:

```python
AgentStreamEvent(thread_id=thread_id, token=token)
```

Consumers keep working unchanged; `thread_id` is a new field on the wire, not a rename.

## Test isolation for node library test suites

`GriptapeNodes` is no longer built by `SingletonMeta`. Its managers now live on an `Engine`
that is resolved per context, so clearing the metaclass cache no longer resets engine state.

If your library's test suite copied this repo's old isolation pattern:

```python
from griptape_nodes.utils.metaclasses import SingletonMeta

SingletonMeta._instances.clear()
```

replace it with:

```python
from griptape_nodes.retained_mode.engine import reset_root_engine

reset_root_engine()
```

This matters even though the old call still imports and runs: it silently stops resetting the
engine, so a patched `USER_CONFIG_PATH` or `ENV_VAR_PATH` set up per test no longer takes
effect after the first test touches the engine. Tests keep passing while reading config from a
previous test's temporary directory.

A test that needs an engine it can hold, rather than a reset between cases, can use
`engine_scope()` from the same module.

# v0.64.0

This guide documents the removal of deprecated nodes from Griptape Nodes libraries in version 0.64.0.

## Overview

Version 0.64.0 removes deprecated nodes that were previously marked for removal. These nodes have been replaced with more flexible and powerful alternatives, primarily the new Diffusion Pipeline Builder system.

### Affected Libraries

| Library                               | Version |
| ------------------------------------- | ------- |
| Griptape Nodes Library                | 0.64.0  |
| Griptape Nodes Advanced Media Library | 0.64.0  |

## Removed Nodes and Replacements

### Image Processing Nodes

| Display Name  | Replacement                                                                                                                                          |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Desaturate    | `Grayscale Image` from Griptape Nodes Library<br/>Location: `Image/Edit/Grayscale Image`<br/>[See details ↓](#for-image-processing-nodes)            |
| Gaussian Blur | `Gaussian Blur Image` from Griptape Nodes Library<br/>Location: `Image/Effects/Gaussian Blur Image`<br/>[See details ↓](#for-image-processing-nodes) |
| Rescale Image | `Rescale Image` from Griptape Nodes Library<br/>Location: `Image/Edit/Rescale Image`<br/>[See details ↓](#for-image-processing-nodes)                |

### Diffusion Pipeline Nodes

All diffusion pipeline nodes have been replaced with the **Diffusion Pipeline Builder** system, which provides a more flexible and composable approach to working with diffusion models.

**Replacement:** Use `Diffusion Pipeline Builder` + `Generate Image (Diffusion Pipeline)` nodes

**Documentation:** https://docs.griptapenodes.com/en/stable/nodes/advanced_media_library/diffusion_pipelines/

**[See migration details ↓](#for-diffusion-pipeline-nodes)**

#### Flux Family

| Display Name      | Category        |
| ----------------- | --------------- |
| Flux              | `image/flux`    |
| Flux Fill         | `image/flux`    |
| Flux Kontext      | `image/flux`    |
| Flux ICEdit       | `image/flux`    |
| Flux Post Upscale | `image/upscale` |

#### Flux ControlNet

| Display Name        | Category                |
| ------------------- | ----------------------- |
| Flux CN Union       | `image/flux/controlnet` |
| Flux CN Union Pro   | `image/flux/controlnet` |
| Flux CN Union Pro 2 | `image/flux/controlnet` |

#### Stable Diffusion Family

| Display Name                       | Category                                   |
| ---------------------------------- | ------------------------------------------ |
| Stable Diffusion                   | `image/stable_diffusion`                   |
| Stable Diffusion 3                 | `image/stable_diffusion_3`                 |
| Stable Diffusion Attend and Excite | `image/stable_diffusion_attend_and_excite` |
| Stable Diffusion DiffEdit          | `image/stable_diffusion_diffedit`          |

#### aMUSEd Family

| Display Name   | Category       |
| -------------- | -------------- |
| aMUSEd         | `image/amused` |
| aMUSEd Img2Img | `image/amused` |
| aMUSEd Inpaint | `image/amused` |

#### Video Generation

| Display Name | Category        |
| ------------ | --------------- |
| Allegro      | `video/allegro` |
| Wan T2V      | `video/wan`     |
| Wan I2V      | `video/wan`     |
| Wan V2V      | `video/wan`     |
| Wan VACE     | `video/wan`     |

#### Audio Generation

| Display Name | Category          |
| ------------ | ----------------- |
| AudioLDM     | `audio/audioldm`  |
| AudioLDM 2   | `audio/audioldm2` |

#### Other Pipelines

| Display Name | Category          |
| ------------ | ----------------- |
| Würstchen    | `image/würstchen` |

### Upscaling Nodes

| Display Name | Replacement                                                                                                                       |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| SPAN Upscale | Use `Diffusion Pipeline Builder` + `Generate Image (Diffusion Pipeline)` nodes<br/>[See details ↓](#for-diffusion-pipeline-nodes) |

### LoRA Nodes

| Display Name   | Replacement                                                                                                                 |
| -------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Flux LoRA File | `Load LoRA` from Griptape Nodes Advanced Media Library<br/>Location: `LoRAs/Load LoRA`<br/>[See details ↓](#for-lora-nodes) |

### Audio Nodes (from Griptape Nodes Library)

| Display Name            | Replacement                                                                                                                                         |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Eleven Music Generation | `Eleven Labs Music Generation` from Griptape Nodes Library<br/>Location: `Audio/Eleven Labs Music Generation`<br/>[See details ↓](#for-audio-nodes) |

## Removed Dependencies

The following Python package dependencies were removed from the Advanced Media Library:

- `beautifulsoup4`
- `protobuf` (duplicate entries consolidated)
- `sentencepiece`
- `torchaudio`
- `ftfy`

## Migration Steps

### For Image Processing Nodes

Replace deprecated nodes with these specific nodes from the main Griptape Nodes Library:

![Grayscale Image node](https://github.com/user-attachments/assets/e3799cb9-bb79-485a-8446-aca639b66aa7) ![Gaussian Blur Image node](https://github.com/user-attachments/assets/8a602390-bfd5-4452-873f-0f8e94cc292e) ![Rescale Image node](https://github.com/user-attachments/assets/925cb3e5-cda5-44b0-9132-99e74e98dda2)

1. **Desaturate** → Replace with **`Grayscale Image`**

    - Location: `Image/Edit/Grayscale Image`
    - Same functionality: converts color images to grayscale
    - Additional features: The new node provides more control over output format, allowing you to choose PNG, JPEG, or WEBP, and adjust quality levels

1. **Gaussian Blur** → Replace with **`Gaussian Blur Image`**

    - Location: `Image/Effects/Gaussian Blur Image`
    - Same functionality: applies gaussian blur with configurable radius
    - Additional features: The new node provides more control over output format, allowing you to choose PNG, JPEG, or WEBP, and adjust quality levels

1. **Rescale Image** → Replace with **`Rescale Image`**

    - Location: `Image/Edit/Rescale Image`
    - **Important:** The new Rescale Image node has changed significantly from the deprecated version
    - The old node used an `nx` scaling basis (scale by 2, 3, 4, etc.)
    - The new node offers multiple resize modes:
        - **percentage** - scales via a percentage basis. If the old node was set to 2, set the new one to 200%
        - **width** - allows you to set the target size for the width of the image. It will maintain aspect ratio for the height
        - **height** - allows you to set the target size for the height of the image. It will maintain aspect ratio for the width
        - **width and height** - allows you to specify both width and height, with options for how to fit the image:
            - **fit** - fits the image within the width and height, maintaining aspect ratio
            - **fill** - crops the image to fill the width and height
            - **stretch** - stretches the image to fit the width and height
    - Additional features: More control over output format (PNG, JPEG, or WEBP) and quality levels

    **Example resize modes:**

    ![Fit mode](https://github.com/user-attachments/assets/b3f6c9c7-f4ef-41bb-a9c8-4a52e0229511) ![Fill mode](https://github.com/user-attachments/assets/39249682-59c8-4ba6-a4dc-ac4dc7804a7a) ![Stretch mode](https://github.com/user-attachments/assets/ab3a14a2-ef5f-44a8-bddf-ec53ae01be3e)

The Grayscale Image and Gaussian Blur Image replacement nodes have equivalent functionality to their deprecated counterparts, with additional output format controls.

### For Diffusion Pipeline Nodes

1. Identify all deprecated diffusion pipeline nodes in your flows
1. Replace each with a combination of:
    - **Diffusion Pipeline Builder** node (configure your model and settings)
    - **Generate Image (Diffusion Pipeline)** node (run the generation)
1. Refer to the [Diffusion Pipeline documentation](https://docs.griptapenodes.com/en/stable/nodes/advanced_media_library/diffusion_pipelines/) for detailed examples
1. The new system offers more flexibility with:
    - Composable pipeline components
    - Reusable pipeline configurations
    - Better control over model loading and optimization

### For LoRA Nodes

1. Replace **Flux LoRA File** with the new `Load LoRA` node
1. The new node is available under `LoRAs/Load LoRA` in the Advanced Media Library
1. Connect the output to your Diffusion Pipeline Builder

### For Audio Nodes

1. Replace **Eleven Music Generation** with **`Eleven Labs Music Generation`**
    - Location: `Audio/Eleven Labs Music Generation` in the main Griptape Nodes Library
    - Same functionality: generates music using the Eleven Labs Music Generation API
    - The replacement node has identical functionality and uses the same underlying API
    - Simply swap the deprecated node for the new one - all parameters work the same way
