# Working with the Project System

The **project system** is Griptape Nodes' centralized file management framework that handles file organization, naming, and saving across all workflows. It eliminates hard-coded file paths and provides a consistent, configurable approach to file operations.

## Overview

Before the project system, nodes used `StaticFilesManager.save_static_file()` with UUID-based filenames scattered across various locations. The project system replaces this with:

- **Centralized configuration**: File organization rules defined in `griptape-nodes-project.yml`
- **Named situations**: Semantic contexts for file operations (e.g., "save_node_output", "copy_external_file")
- **Template-based paths**: Dynamic path generation using macros like `{outputs}/{node_name}_{file_name_base}.{file_extension}`
- **Consistent behavior**: All nodes automatically follow the same file layout

## Key Components

### Workspace

The root directory containing all project work. Configured in Griptape Nodes settings, it serves as the base for relative path resolution.

```
workspace/
├── griptape-nodes-project.yml    # Optional customizations
├── my_workflow/
│   ├── inputs/
│   ├── outputs/
│   ├── temp/
│   └── .griptape-nodes-previews/
```

### Situations

Named scenarios that define:

1. **Where** files are saved (via macro templates)
1. **How** to handle collisions (create_new, overwrite, fail)
1. **Fallback** behavior if saving fails

Common situations include:

| Situation            | Purpose                | Default Macro Pattern                                                   |
| -------------------- | ---------------------- | ----------------------------------------------------------------------- |
| `save_node_output`   | Generated node outputs | `{outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}` |
| `copy_external_file` | External file imports  | `{inputs}/{node_name?:_}{parameter_name?:_}{file_name_base}...`         |
| `download_url`       | Downloaded files       | `{inputs}/{sanitized_url}`                                              |
| `save_preview`       | Thumbnail generation   | `{previews}/{source_relative_path?:/}...`                               |

### Macros

Template strings in situations and directories that generate concrete file paths dynamically. Examples:

- `{outputs}` - resolves to the outputs directory path
- `{node_name}` - current node's name
- `{file_name_base}` - filename without extension
- `{file_extension}` - file extension
- `{_index?:03}` - auto-incrementing counter (3-digit format)

### Directories

Logical name-to-path mappings that can be referenced in macros:

```yaml
directories:
  outputs:
    path_macro: "outputs"
  custom_renders:
    path_macro: "my_custom_path/{workflow_name}"
```

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

## Advanced Configuration

Users can customize the project system by creating a `griptape-nodes-project.yml` file in their workspace:

```yaml
project_template_schema_version: "0.1.0"
name: "My Custom Project"

directories:
  outputs:
    path_macro: "final_outputs/{workflow_name}"

situations:
  save_node_output:
    macro: "{outputs}/{node_name}_{file_name_base}_{_index:04}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
```

Your nodes automatically respect these customizations without any code changes.

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
