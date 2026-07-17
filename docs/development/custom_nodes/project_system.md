# Working with the Project System

The **project system** is Griptape Nodes' centralized file management framework that handles file organization, naming, and saving across all workflows. It eliminates hard-coded file paths and provides a consistent, configurable approach to file operations. This page covers the node-developer side: how a node saves files through the project system.

## Concepts

The concepts behind the project system — the workspace, project templates, situations, macros, directories, and environment variables — are documented in the [Project system guides](../../guides/projects/index.md):

- [Overview](../../guides/projects/index.md) — how the pieces fit together
- [Situations](../../guides/projects/situations.md) — named file-saving scenarios, collision policies, and the full table of default situations
- [Macros](../../guides/projects/macros.md) — the template syntax used to build file paths
- [Directories](../../guides/projects/directories.md) — logical name-to-path mappings referenced in macros
- [Customization Guide](../../guides/projects/customization.md) — how users override paths and situations via `griptape-nodes-project.yml`

The short version for node authors: a **situation** names a file-saving scenario (e.g. `save_node_output`), its **macro** template (e.g. `{outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}`) generates the concrete path, and users can customize all of it without any changes to your node's code.

## Using the Project System in Nodes

There are two main patterns for working with project files in nodes:

### Pattern 1: ProjectFileParameter (Recommended for Node Outputs)

Use `ProjectFileParameter` when your node has a configurable output file parameter that users might want to customize.

```python
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

class MyVideoNode(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Add regular output parameter
        self.add_parameter(Parameter(
            name="output_video",
            output_type="VideoUrlArtifact",
            tooltip="Generated video",
            allowed_modes={ParameterMode.OUTPUT}
        ))

        # Add project file parameter for output file configuration
        self._output_video_file = ProjectFileParameter(
            node=self,
            name="output_video_file",
            default_filename="output_video.mp4",
        )
        self._output_video_file.add_parameter()

    def process(self) -> None:
        # ... generate video_bytes ...

        # Use build_file() to get a ProjectFileDestination
        dest = self._output_video_file.build_file()
        saved = dest.write_bytes(video_bytes)

        # Set the output parameter with the saved location
        self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)
```

**Key Points:**

- `ProjectFileParameter` creates a UI parameter that users can configure
- Call `build_file()` to get a `ProjectFileDestination` instance
- Use `write_bytes()` to save the file
- Access the saved file's URL/path via `saved.location`

**❌ Common Mistake: Not Capturing write_bytes() Return Value**

```python
# WRONG - Don't do this:
dest = self._output_video_file.build_file()
dest.write_bytes(video_bytes)  # ❌ Return value not captured
artifact = VideoUrlArtifact(dest.location)  # Using dest, not saved file

# This will fail with: "Failed because missing required variables: file_extension, file_name_base"
```

**Why it fails:** Macro variables like `{file_extension}` and `{file_name_base}` are resolved when `write_bytes()` saves the file and returns the saved file object, not by `build_file()`. Using `dest.location` before writing causes macro resolution errors.

```python
# CORRECT:
dest = self._output_video_file.build_file()
saved = dest.write_bytes(video_bytes)  # ✅ Capture return value
artifact = VideoUrlArtifact(saved.location)  # Use saved file's resolved location
```

The `saved` object contains the fully resolved file path with all macros filled in.

### Pattern 2: ProjectFileDestination Directly (For Utility Functions)

Use `ProjectFileDestination.from_situation()` directly in utility functions or when you don't need user configuration.

```python
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

def frames_to_video_artifact(frames: list, fps: int = 30, video_format: str = "mp4") -> VideoUrlArtifact:
    """Convert a list of frames to a VideoUrlArtifact."""
    # ... process frames into video_bytes ...

    # Save using project file system
    dest = ProjectFileDestination.from_situation(
        filename=f"video.{video_format}",
        situation="save_node_output"
    )
    saved = dest.write_bytes(video_bytes)

    return VideoUrlArtifact(saved.location)
```

**Key Points:**

- Use `from_situation()` to create a destination with a named situation
- The `filename` parameter is the base filename (will be transformed by the situation's macro)
- The situation (e.g., "save_node_output") determines the final path and collision behavior

## Migration from StaticFilesManager

### Old Pattern (Deprecated)

```python
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
import uuid

def old_save_video(video_bytes: bytes) -> VideoUrlArtifact:
    filename = f"{uuid.uuid4()}.mp4"
    url = GriptapeNodes.StaticFilesManager().save_static_file(video_bytes, filename)
    return VideoUrlArtifact(url)
```

### New Pattern

```python
from griptape_nodes.files.project_file import ProjectFileDestination

def new_save_video(video_bytes: bytes) -> VideoUrlArtifact:
    dest = ProjectFileDestination.from_situation(
        filename="video.mp4",
        situation="save_node_output"
    )
    saved = dest.write_bytes(video_bytes)
    return VideoUrlArtifact(saved.location)
```

**Migration Benefits:**

- No more UUID generation required
- Consistent file organization across all nodes
- User-configurable file paths via project templates
- Better file tracking and management
- Automatic handling of name collisions

## Common Situations and When to Use Them

- **`save_node_output`**: Primary situation for files generated by nodes (images, videos, audio, etc.)
- **`copy_external_file`**: When importing/copying files from external sources
- **`download_url`**: When downloading files from URLs
- **`save_preview`**: For generating thumbnail or preview images
- **`save_static_file`**: For static assets that don't change between runs

See [Situations](../../guides/projects/situations.md#default-situations) for the full table of default situations, their macros, and collision policies.

Users can override any of these (paths, macros, collision policies) through their project's `griptape-nodes-project.yml` — see the [Customization Guide](../../guides/projects/customization.md). Your nodes automatically respect these customizations without any code changes.

## Best Practices

1. **Always use the project system** for saving files - never use hard-coded paths
1. **Choose the right pattern**: Use `ProjectFileParameter` for user-configurable outputs, `ProjectFileDestination` for utility functions
1. **Use semantic situations**: Pick the situation that best describes your operation
1. **Let macros handle naming**: Don't generate UUIDs or timestamps yourself - let the situation's macro and collision policy handle it
1. **Handle temporary files properly**: Use Python's `tempfile` for intermediate processing, only save final results via the project system
1. **Clean up temporary files**: Always clean up temporary files after copying to the project system

## Example: Complete Video Processing Node

```python
import tempfile
from pathlib import Path
from typing import Any

from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, AsyncResult
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.files.file import File

class ProcessVideo(ControlNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        self.add_parameter(Parameter(
            name="input_video",
            input_types=["VideoUrlArtifact"],
            type="VideoUrlArtifact",
            tooltip="Input video to process"
        ))

        self.add_parameter(Parameter(
            name="output_video",
            output_type="VideoUrlArtifact",
            tooltip="Processed video",
            allowed_modes={ParameterMode.OUTPUT}
        ))

        # Add project file parameter for output
        self._output_video_file = ProjectFileParameter(
            node=self,
            name="output_video_file",
            default_filename="processed_video.mp4",
        )
        self._output_video_file.add_parameter()

    def process(self) -> AsyncResult:
        yield lambda: self._process()

    def _process(self) -> None:
        # Get input video
        input_artifact = self.get_parameter_value("input_video")
        input_bytes = File(input_artifact.value).read_bytes()

        # Process in temporary location
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        try:
            # Write input to temp file
            temp_path.write_bytes(input_bytes)

            # ... perform processing on temp_path ...

            # Read processed result
            output_bytes = temp_path.read_bytes()

            # Save using project system
            dest = self._output_video_file.build_file()
            saved = dest.write_bytes(output_bytes)

            # Set output
            self.parameter_output_values["output_video"] = VideoUrlArtifact(saved.location)

        finally:
            # Clean up temporary file
            if temp_path.exists():
                temp_path.unlink()
```
