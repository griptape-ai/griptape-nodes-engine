# Node Libraries

This section documents **node libraries** — installable bundles of
nodes that extend Griptape Nodes. The [Standard Library](../nodes/overview.md)
ships installed by default; every other library lives in its own Git
repository and is installed through the editor's **Libraries** panel or the
`gtn` CLI.

For how installation, updating,
dependency isolation, and Shared/Isolated modes work, see the
[Libraries guide](../guides/libraries.md).

## Documented libraries

| Library                                           | Nodes | What it's for                                                                                                          | Requirements                                              |
| ------------------------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| [Standard Library](../nodes/overview.md)          | 284   | The default nodes — agents, text, images, video, audio, lists, dicts, JSON, execution flow, and more                   | None (installed by default)                               |
| [Advanced Media Library](advanced_media/index.md) | 28    | Local media generation and manipulation — diffusion pipelines, image/video aux processing, LoRAs, face detection       | GPU recommended; Hugging Face account for model downloads |
| [OpenColorIO](opencolorio/index.md)               | 2     | Professional color management using OCIO configs — the industry-standard system for film, VFX, and animation pipelines | An OCIO config (`$OCIO` or explicit path)                 |
| [OpenEXR](openexr/index.md)                       | 4     | Load, inspect, display, and save OpenEXR files for VFX and HDR image workflows                                         | None (optionally pairs with OpenColorIO)                  |
| [Nuke](nuke/index.md)                             | 3     | Run `.nk` scripts headlessly from the canvas and publish workflows as versioned Nuke gizmos                            | A local Foundry Nuke installation and license             |
| [Diffusers](diffusers/index.md)                   | 19    | Modular 🧨 Diffusers pipelines — build media generation workflows from individual, connectable diffusion stages        | GPU (CUDA or MPS)                                         |

This is not an exhaustive list — many more first-party and community
libraries are available in the
[Griptape Nodes directory](https://github.com/griptape-ai/griptape-nodes-directory).
You can also browse them from the editor via the **Browse Community
Libraries** button in the **Add Library** modal.

## Installing a library

The Standard Library is registered automatically, and the Advanced Media
Library is offered during `gtn init`. Every other library on this page
installs the same way:

1. In the editor, open **Manage → Library Management**.
1. Click **Add Library** and paste the library's Git URL (listed on each
    library's overview page).
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

## How these docs are organized

Each library gets:

- An **overview page** — what the library does, how to install it, any
    prerequisites (environment variables, hardware, accounts), and its settings.
- A **reference page per node** — a consistent format with a TL;DR, the node's
    typical position in a workflow, and inputs/outputs/parameters tables.
