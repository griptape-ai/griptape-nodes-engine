# Advanced Media Library

Advanced media generation and manipulation nodes that run models **locally** —
diffusion pipelines, image and video pre-processing (depth, edge, pose,
segmentation), LoRA tooling, and face detection.

- **Repository**: [griptape-ai/griptape-nodes-library-advanced-media](https://github.com/griptape-ai/griptape-nodes-library-advanced-media)
- **Requirements**: a GPU is strongly recommended; a Hugging Face account and
    access token for model downloads (see
    [Hugging Face Models](../../guides/integrations/hugging_face.md))
- **Node categories**: audio, diffusion, image, video, LoRA, and utils in the
    node picker

## Installation

The Advanced Media Library is offered during `gtn init` — you can register it
then, or re-run `gtn init` later and answer `y`. See the
[FAQ](../../faq.md#how-do-i-install-the-advanced-media-library-after-initial-setup)
for that path.

Alternatively, install it like any other library: in the editor, open
**Manage → Library Management**, click **Add Library**, and paste:

```text
https://github.com/griptape-ai/griptape-nodes-library-advanced-media
```

See the [Libraries guide](../../guides/libraries.md) for general install,
update, and troubleshooting help.

## Node reference

The library provides 28 nodes. The ones with reference pages so far:

- [Diffusion Pipelines](../../nodes/advanced_media_library/diffusion_pipelines.md)
    — build and cache 🤗 Diffusers pipelines, then generate images with them
- [YOLOv8 Face Detection](../../nodes/advanced_media_library/yolov8_face_detection.md)
    — detect faces and emit bounding boxes/masks

!!! warning "Using alongside the Diffusers Library"

    The Advanced Media Library and the [Diffusers Library](../diffusers/index.md)
    share upstream dependencies (`diffusers`, `transformers`, `torch`) and each
    maintains its own in-memory pipeline cache. Using both in the same session
    can hold two copies of overlapping model weights in VRAM/RAM at once.
    Prefer one library per session.

## Support

Found a bug or have a feature request?
[Open an issue](https://github.com/griptape-ai/griptape-nodes-library-advanced-media/issues).
