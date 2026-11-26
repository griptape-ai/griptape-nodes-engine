# Node Libraries

Node libraries are installable bundles of nodes that extend Griptape Nodes.
The Standard Library is installed by default. Every other library lives in its
own Git repository and is installed through the editor's **Libraries** panel
or the `gtn` CLI.

For how installation, updating, dependency isolation, and process isolation
work, see the [Libraries guide](../guides/libraries.md).

## Library documentation

The docs for each library, including a page for each node, ship in the
library's own `docs/` folder. From engine 0.110.0 they show in the editor's
**Docs** panel. On an older engine, read them on GitHub. The pages are plain
markdown.

- **Standard Library**: The default nodes: agents, text, images, video, audio, lists, dicts, JSON, execution flow, and more. Installed by default.
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-standard/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-standard/blob/main/docs/index.md)
- **Advanced Media**: Local media generation and manipulation: diffusion pipelines, image and video aux processing, LoRAs, face detection. A GPU is recommended.
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-advanced-media/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-advanced-media/blob/main/docs/index.md)
- **Diffusers**: Modular Diffusers pipelines. Build media generation workflows from individual, connectable diffusion stages. Needs a GPU (CUDA or MPS).
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-diffusers/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-diffusers/blob/main/docs/index.md)
- **Nuke**: Run `.nk` scripts headlessly from the canvas and publish workflows as versioned Nuke gizmos. Needs a local Foundry Nuke installation and license.
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-nuke/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-nuke/blob/main/docs/index.md)
- **OpenColorIO**: Color management with OCIO configs, the standard for film, VFX, and animation pipelines. Needs an OCIO config.
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-opencolorio/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-opencolorio/blob/main/docs/index.md)
- **OpenEXR**: Read, inspect, display, and save OpenEXR files and frame sequences for VFX and HDR image workflows. Needs engine 0.104.0 or newer.
    [Docs folder](https://github.com/griptape-ai/griptape-nodes-library-openexr/tree/main/docs) | [docs/index.md](https://github.com/griptape-ai/griptape-nodes-library-openexr/blob/main/docs/index.md)

Many more first-party and community libraries are available in the
[Griptape Nodes directory](https://github.com/griptape-ai/griptape-nodes-directory).
You can also browse them from the editor via the **Browse Community
Libraries** button in the **Add Library** modal.

## Installing a library

The Standard Library is registered automatically, and the Advanced Media
Library is offered during `gtn init`. Every other library on this page
installs the same way:

1. In the editor, open **Manage → Library Management**.
1. Click **Add Library** and paste the library's Git URL.
1. Click **Install**. The engine clones the repository, installs the library's
    Python dependencies into an isolated virtual environment, and registers its
    nodes.

Or from the command line:

```bash
gtn libraries download <git_url>
```

Each library's dependencies are isolated from the engine and from every other
library, so installing any combination of these libraries is safe. See
[Coexistence guarantees](../guides/libraries.md#coexistence-guarantees) for
details.
