# Authoring Libraries

Nodes are distributed as libraries. This page covers the library manifest (`griptape_nodes_library.json`), declarations, dependency management, documentation conventions, and the process for contributing nodes to the standard library.

## Creating Node Libraries

Bundle nodes into libraries for sharing. Create `griptape_nodes_library.json`:

```json
{
  "name": "Library Name",
  "library_schema_version": "0.10.0",
  "settings": [
    {
      "description": "API keys required by nodes in this library",
      "category": "app_events.on_app_initialization_complete",
      "contents": {
        "secrets_to_register": ["MY_SERVICE_API_KEY", "MY_OTHER_API_KEY"]
      }
    }
  ],
  "metadata": {
    "author": "Author Name",
    "description": "Library description",
    "library_version": "1.0.0",
    "engine_version": "0.55.0",
    "tags": ["AI", "Image Processing"],
    "dependencies": {
      "pip_dependencies": ["pillow", "requests"],
      "pip_install_flags": ["--upgrade"]
    },
    "declarations": [
      { "type": "lifecycle_stage", "stage": "STABLE" },
      {
        "type": "model_catalog",
        "providers": {
          "anthropic": {
            "display_name": "Anthropic",
            "terms_url": "https://www.anthropic.com/legal/commercial-terms",
            "models": {
              "claude_opus_byok": {
                "display_name": "Claude Opus 4 (BYOK)",
                "family": "Claude 4",
                "provider_model_id": "claude-opus-4",
                "key_support": "REQUIRES_CUSTOMER_KEY"
              }
            }
          }
        }
      }
    ]
  },
  "widgets": [
    {
      "name": "MyWidget",
      "path": "widgets/MyWidget.js",
      "description": "Custom UI component for the node"
    }
  ],
  "categories": [
    {
      "image": {
        "title": "Image Processing",
        "description": "Image manipulation nodes",
        "color": "border-purple-500",
        "icon": "Image"
      }
    }
  ],
  "nodes": [
    {
      "class_name": "MyImageNode",
      "file_path": "image/my_image_node.py",
      "metadata": {
        "category": "image",
        "description": "Process images with AI",
        "display_name": "AI Image Processor",
        "icon": "image",
        "group": "processing",
        "declarations": [
          { "type": "model_usage", "model_ids": ["claude_opus_byok"] }
        ]
      }
    }
  ],
  "workflows": ["workflows/example_workflow.py"],
  "is_default_library": false
}
```

### Library Structure

- **settings**: Register secrets/API keys used by library nodes
    - Use `secrets_to_register` array to declare required secrets
    - Category should be `app_events.on_app_initialization_complete`
    - Secrets are accessed via `GriptapeNodes.SecretsManager().get_secret()`
- **metadata.dependencies**: PIP packages installed on library load
- **metadata.declarations** / per-node **metadata.declarations**: typed identity properties (lifecycle stage, arbitrary Python execution) and a library-level model catalog plus per-node references into it. See [Library and Node Declarations](#library-and-node-declarations) below.
- **widgets**: Register custom JS widget components (see [Custom Widgets](custom_widgets.md))
- **categories**: Group nodes in UI with colors and icons
- **nodes**: List node classes, file paths, and metadata
- **workflows**: Template workflow files

**Important:** The `secrets_to_register` array tells the system which secrets your library needs. Users will be prompted to configure these secrets through the UI or environment variables.

Use flat directory structures. The engine automatically registers and loads libraries.

### Library and Node Declarations

Declarations attach typed metadata to a library or to an individual node. Each entry in a `declarations` array is an object with a `type` discriminator that selects a declaration class. Today's vocabulary covers a lifecycle-stage property, a library-level model catalog with per-node references, and an arbitrary-Python-execution property; future engine releases add more declaration types under the same field.

Both `metadata.declarations` (library-level) and per-node `metadata.declarations` accept a list. Order in the list does not matter. The field defaults to `[]`, so libraries on older schema versions (`0.6.0`, `0.4.0`, `0.1.0`) load unchanged.

#### `lifecycle_stage`

Lifecycle stage for a library or for a specific node. Values:

| Value        | Meaning                                                             |
| ------------ | ------------------------------------------------------------------- |
| `STABLE`     | Mature; intended for production use.                                |
| `BETA`       | Feature-complete but still hardening.                               |
| `ALPHA`      | Early implementation; expect breaking changes.                      |
| `LABS`       | Exploratory; may be removed.                                        |
| `DEPRECATED` | Slated for removal; existing usage should migrate to a replacement. |

Semantics:

- **Library-level absence is intentionally distinct from `STABLE`.** A library with no `lifecycle_stage` declaration is "unstated" — consumers should surface that explicitly (`<No lifecycle stage provided by library author>`) rather than silently assume `STABLE`.
- **Node-level absence means "inherit the library's stage."** A node-level `lifecycle_stage` overrides the library's value.

#### `model_catalog`

A library-level declaration of the third-party models nodes in the library can use, organized as a `provider → model` registry. Identifiers at both levels are dict keys (the key *is* the stable handle used by node references and admin policies); each entry carries a `display_name` for UI plus optional `terms_url` and `notes`. Each `Model` additionally declares `key_support` (required), an optional `family` grouping tag, and an optional upstream `provider_model_id`.

The `key_support` value tells admins what kind of API key authorizes the call:

| Value                                   | Meaning                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| `REQUIRES_CUSTOMER_KEY`                 | Customer-supplied API key only.                                                     |
| `SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY` | Customer key or Griptape-provided key both work.                                    |
| `REQUIRES_GRIPTAPE_KEY`                 | Griptape-provided key only.                                                         |
| `NO_KEY_REQUIRED`                       | The model runs locally or otherwise needs no API key (e.g. an Ollama-hosted model). |

`notes` (available on both the provider and the model) is free-form author guidance rendered alongside the entry. Use it for caveats that don't fit other fields, like "BYOK requires injecting a provider-specific prompt driver."

```jsonc
{
  "type": "model_catalog",
  "providers": {
    "anthropic": {
      "display_name": "Anthropic",
      "terms_url": "https://www.anthropic.com/legal/commercial-terms",
      "models": {
        "claude_opus_byok": {
          "display_name": "Claude Opus 4 (BYOK)",
          "family": "Claude 4",
          "provider_model_id": "claude-opus-4",
          "key_support": "REQUIRES_CUSTOMER_KEY"
        },
        "claude_opus_griptape": {
          "display_name": "Claude Opus 4 (Griptape Key)",
          "family": "Claude 4",
          "provider_model_id": "claude-opus-4",
          "key_support": "REQUIRES_GRIPTAPE_KEY"
        }
      }
    },
    "kling": {
      "display_name": "Kling",
      "terms_url": "https://app.klingai.com/global/about/terms",
      "models": {
        "kling_v2": {
          "display_name": "Kling v2",
          "provider_model_id": "kling-v2-master",
          "key_support": "REQUIRES_GRIPTAPE_KEY"
        }
      }
    },
    "ollama": {
      "display_name": "Ollama",
      "key_support": "NO_KEY_REQUIRED",
      "notes": "Local runtime; models enumerated at runtime, none declared here."
    }
  }
}
```

A few rules worth knowing:

- **`family` is just a tag.** It clusters related models for display (e.g. the two Claude 4 entries above). It is not a container and is not part of a model's identity, so providers without meaningful families simply omit it.
- **`key_support` lives on the model by default.** Every `Model` declares its own value. The same upstream model with two different key requirements becomes two models under two distinct dict keys (see `claude_opus_byok` and `claude_opus_griptape` above). A `ModelProvider` also accepts an optional `key_support` used only when it declares no models at all (e.g. a local-runtime provider like Ollama where `key_support=NO_KEY_REQUIRED` is the only meaningful signal).
- **Model IDs must be unique across the entire library.** Pydantic enforces uniqueness within each provider's `models` dict for free; collisions across providers are caught at library-load time as `DuplicateModelIdProblem`.
- **At most one `model_catalog` per library.** Declaring two is rejected at validation time; merge their providers into one.

##### `model_usage`

A node references one or more catalog models by their dict keys. Use this when the node binds to a specific, named set of models. Each entry must resolve to a model somewhere in the catalog at library-load time; unresolved references surface as `UnresolvedModelUsageReferenceProblem`.

```jsonc
{ "type": "model_usage", "model_ids": ["claude_opus_byok", "kling_v2"] }
```

##### `model_provider_usage`

A node references one or more entire providers. Use this when a node dynamically enumerates every model a provider offers at runtime. Each entry must resolve to a provider declared in the catalog; unresolved references surface as `UnresolvedModelProviderUsageReferenceProblem`.

```jsonc
{ "type": "model_provider_usage", "provider_ids": ["anthropic", "ollama"] }
```

The two usage declarations are independent. A node can carry any combination — for instance, "every model this provider offers, plus these two specific models from another provider."

#### `arbitrary_python_execution`

Declares that a node executes arbitrary Python code supplied at runtime (for example, an artist-authored script). Node-level only. This is a security-relevant identity fact: consumers (UI) can warn an artist before the node runs. Absence of this declaration means the node does not execute arbitrary Python.

| Field                       | Meaning                                                      |
| --------------------------- | ------------------------------------------------------------ |
| `executes_arbitrary_python` | `true` when the node runs unvetted, runtime-supplied Python. |

```jsonc
"declarations": [
  { "type": "arbitrary_python_execution", "executes_arbitrary_python": true }
]
```

#### Combining declarations

A node can carry any combination of declarations. For example, a Labs node that uses two models:

```jsonc
"metadata": {
  "category": "labs",
  "description": "Labs node demonstrating multiple declarations.",
  "display_name": "Labs Node",
  "declarations": [
    { "type": "lifecycle_stage", "stage": "LABS" },
    { "type": "model_usage", "model_ids": ["claude_opus_byok", "kling_v2"] }
  ]
}
```

New declaration types added in future engine releases land additively under this same `declarations` field without a schema-version bump.

## Library Structure with uv Dependency Management

**Modern Approach**: Use `uv` for fast, reproducible dependency management following the Minimax library pattern.

### Directory Structure

```
library-name/
├── pyproject.toml              # uv configuration
├── uv.lock                     # Lock file (generated)
├── LICENSE                     # License file
├── README.md                   # Documentation
├── CHANGELOG.md                # Version history
├── .gitignore                  # Ignore rules
└── library_name/
    ├── griptape_nodes_library.json  # Library metadata
    └── node_file.py
```

### pyproject.toml Configuration

```toml
[project]
name = "library-name"
version = "1.0.0"
description = "Description of your library"
authors = [
    {name = "Your Name", email = "email@example.com"}
]
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "griptape-nodes-engine",
    "requests",
    # Add other dependencies
]

[tool.uv.sources]
griptape-nodes-engine = { git = "https://github.com/griptape-ai/griptape-nodes", rev="latest"}

[tool.hatch.build.targets.wheel]
packages = ["library_name"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### Library Configuration (inside subdirectory)

Place `griptape_nodes_library.json` inside the library subdirectory:

```json
{
  "name": "Library Name",
  "library_schema_version": "0.1.0",
  "settings": [
    {
      "description": "API keys required by nodes",
      "category": "app_events.on_app_initialization_complete",
      "contents": {
        "secrets_to_register": ["API_KEY_NAME"]
      }
    }
  ],
  "nodes": [
    {
      "class_name": "NodeClassName",
      "file_path": "node_file.py", // Relative to library subdirectory
      "metadata": {
        "category": "category_name",
        "description": "Node description",
        "display_name": "Node Display Name"
      }
    }
  ]
}
```

### Installation Instructions in README

Provide both uv (recommended) and pip (fallback) installation methods:

````markdown
## Installation

### Option 1: Using uv (Recommended)

1. Clone or download this library

2. Install dependencies:

   ```bash
   cd library-name
   uv sync
   ```

3. Place in Griptape Nodes libraries directory

### Option 2: Automatic Installation

1. Place folder in libraries directory
2. Dependencies install automatically via pip
````

### Generate Lock File

```bash
cd library-name
uv sync
```

**Benefits:**

- Fast installation (Rust-based)
- Reproducible builds via lock file
- Direct GitHub integration for griptape-nodes
- Backward compatible with pip installation

## Documentation Patterns for Node Libraries

### Comprehensive README Structure

```markdown
# Library Name

Brief description and key features.

## Features

- Bullet list of main capabilities
- Include model options
- Highlight unique features

## Installation

### Option 1: Using uv (Recommended)

Steps for uv installation

### Option 2: Automatic Installation

Steps for pip installation

## Getting Started

### Simple Mode (Recommended for First-Time Users)

Minimal example with explanations

### Custom Mode (Advanced)

Advanced example showing all features

## Parameters

### Basic Parameters

Table with Name, Type, Description

### Advanced Parameters (Hidden by Default)

Table with Name, Type, Default, Description

### Output Parameters

Table with outputs

## Model Comparison

Table comparing models:
| Model | Max Duration | Quality | Speed | Character Limits |

## Character Limits

Clear tables showing limits by model/mode

## API Rate Limits

Document:

- Concurrency limits
- Generation time expectations
- File retention policies

## Example Workflows

3-5 complete examples covering common use cases

## Error Handling

Common errors and solutions

## Troubleshooting

FAQ-style troubleshooting guide

## API Reference

Link to official API docs

## Best Practices

Tips for optimal usage

## Support

Where to get help

## Version History

Link to CHANGELOG
```

### Troubleshooting Entries Worth Documenting

Two errors come up often enough that a library README's troubleshooting section should cover them:

#### Error: "Missing required variables: file_extension, file_name_base"

**Full Error:**

```text
ERROR: Attempted to resolve macro path. Failed because missing required variables: file_extension, file_name_base
ERROR: Attempted to create download URL. Failed with file_path='{outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}'
```

**Cause:** Not capturing the return value from `write_bytes()`. Using `dest.location` instead of `saved.location`.

**Incorrect Code:**

```python
dest = self._output_file.build_file()
dest.write_bytes(video_bytes)  # ❌ Return value not captured
artifact = VideoUrlArtifact(dest.location)  # Using dest, not saved file
```

**Solution:**

```python
dest = self._output_file.build_file()
saved = dest.write_bytes(video_bytes)  # ✅ Capture the saved file
artifact = VideoUrlArtifact(saved.location)  # Use saved file's resolved location
```

**Explanation:** Macro variables are populated when `write_bytes()` actually saves the file. The `saved` object returned by `write_bytes()` contains the fully resolved path.

#### Error: Type Conversion Issues with Image Parameters

**Symptom:** Image parameters don't handle different input types consistently, or errors occur when passing URLs, file paths, or artifacts between nodes.

**Problem:** Using generic `Parameter` with manual type configuration doesn't standardize type conversion logic:

```python
# ❌ Inconsistent type handling
Parameter(
    name="image",
    input_types=["ImageArtifact", "ImageUrlArtifact", "str"],
    type="ImageArtifact",
)
```

**Solution:** Use `ParameterImage` for standardized type conversion:

```python
# ✅ Standardized type handling
ParameterImage(
    name="image",
    tooltip="Input image",
    allow_output=False,
)
```

**Benefits:**

- Handles ImageArtifact, ImageUrlArtifact, and strings consistently
- Built-in support for URLs, file paths, and data URIs
- Graceful error handling for various input formats
- Reduces type conversion errors in complex workflows

### Model Comparison Table

Always include a comparison table for services with multiple models:

```markdown
| Model | Max Duration | Quality  | Speed   | Character Limits          |
| ----- | ------------ | -------- | ------- | ------------------------- |
| V5    | 4 min        | Superior | Fastest | Prompt: 5000, Style: 1000 |
| V4_5  | 8 min        | High     | Fast    | Prompt: 5000, Style: 1000 |
| V4    | 4 min        | Best     | Medium  | Prompt: 3000, Style: 200  |
```

## Contributing to the Standard Library

When adding nodes to the core `griptape_nodes_library` (as opposed to creating a standalone library), follow this process:

### 1. Create a Feature Branch

```bash
cd griptape-nodes
git checkout -b feature/add-color-match-node
```

### 2. Add the Node File

Place your node in the appropriate category subdirectory:

```
libraries/griptape_nodes_library/griptape_nodes_library/
├── image/
│   ├── color_match.py      # New node file
│   ├── load_image.py
│   └── save_image.py
├── text/
├── audio/
└── ...
```

### 3. Update griptape_nodes_library.json

Make three updates to `libraries/griptape_nodes_library/griptape_nodes_library.json`:

#### a. Increment the library version

```json
{
  "metadata": {
    "library_version": "0.59.0" // Was 0.58.0
  }
}
```

#### b. Add any new pip dependencies

```json
{
  "metadata": {
    "dependencies": {
      "pip_dependencies": [
        "existing-dep",
        "color-matcher" // New dependency
      ]
    }
  }
}
```

#### c. Add the node entry

```json
{
  "nodes": [
    {
      "class_name": "ColorMatch",
      "file_path": "griptape_nodes_library/image/color_match.py",
      "metadata": {
        "category": "image",
        "description": "Transfer color characteristics from a reference image to a target image",
        "display_name": "Color Match",
        "icon": "palette",
        "group": "edit"
      }
    }
  ]
}
```

### 4. Add Documentation

Create a documentation page at `docs/nodes/<category>/<node_name>.md`:

```markdown
# Color Match

Transfer color characteristics from a reference image to a target image.

## What It Does

Applies the color palette from a reference image to a target image...

## Parameters

### Inputs

| Parameter       | Type             | Description                 |
| --------------- | ---------------- | --------------------------- |
| reference_image | ImageUrlArtifact | Source of the color palette |
| target_image    | ImageUrlArtifact | Image to apply colors to    |

### Outputs

| Parameter    | Type             | Description          |
| ------------ | ---------------- | -------------------- |
| output_image | ImageUrlArtifact | Color-matched result |

## Example Usage

1. Connect a reference image with desired colors
2. Connect the target image to transform
3. Run the node

## Technical Details

Uses the color-matcher library with histogram matching...
```

### 5. Update mkdocs.yml Navigation

Add your doc page to the navigation in `mkdocs.yml`:

```yaml
nav:
  - Nodes Reference:
      - Image:
          - Load Image: nodes/image/load_image.md
          - Save Image: nodes/image/save_image.md
          - Color Match: nodes/image/color_match.md # New entry
```

### 6. Run Quality Checks

Before committing, run formatting and checks:

```bash
make format        # Auto-format code
make check/lint    # Check for linting issues
make check/types   # Check for type errors
```

Fix any issues that arise before proceeding.

### 7. Commit and Create PR

```bash
git add .
git commit -m "feat(image): add ColorMatch node for color transfer"
git push -u origin HEAD
gh pr create --title "Add ColorMatch node" --body "## Summary
- Adds ColorMatch node for transferring colors between images
- Uses color-matcher library
- Includes documentation

## Test plan
- [ ] Load two images
- [ ] Run color match
- [ ] Verify output has reference colors"
```

### Standard Library vs External Library

| Aspect       | Standard Library                            | External Library                  |
| ------------ | ------------------------------------------- | --------------------------------- |
| Location     | `griptape-nodes` repo                       | Separate repo                     |
| Installation | Included by default                         | User installs                     |
| Review       | Requires PR approval                        | Self-published                    |
| Dependencies | Added to core `griptape_nodes_library.json` | Own `griptape_nodes_library.json` |
| Versioning   | Follows core library version                | Independent versioning            |
| Docs         | Added to main docs site                     | README in library                 |

**When to contribute to standard library:**

- Node has broad utility for many users
- No proprietary/paid API dependencies
- Stable, well-tested implementation
- Follows all code quality standards

**When to create external library:**

- Niche use case
- Requires paid API keys
- Experimental/rapidly changing
- Want independent release cycle
