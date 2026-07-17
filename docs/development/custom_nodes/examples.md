# Patterns and Examples

A collection of advanced patterns and reference material: API integration approaches, UI/UX patterns from production nodes, flexible artifact processing, and quick-reference tables for imports, utilities, and types.

## Advanced Topics

### REST API vs SDK Integration

**Problem**: Python SDKs often lag behind REST APIs in supporting new features. Parameters available in REST API documentation may not be exposed in SDK libraries.

**When to Use REST API Directly**:

- SDK missing documented API features (e.g., `image_config` for Gemini)
- Need immediate access to new API parameters
- SDK has bugs or limitations
- Want lighter dependencies

**REST API Implementation Pattern**:

```python
import base64
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# Authentication
credentials = service_account.Credentials.from_service_account_file(
    service_account_file,
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)

def _get_access_token(self, credentials) -> str:
    """Get access token from credentials."""
    if not credentials.valid:
        credentials.refresh(Request())
    return credentials.token

# Build JSON payload matching REST API spec
payload = {
    "contents": {
        "role": "USER",
        "parts": [
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode('utf-8')
                }
            }
        ]
    },
    "generation_config": {
        "temperature": 1.0,
        "topP": 0.95,
        "candidateCount": 1,
        "response_modalities": ["TEXT", "IMAGE"],
        "image_config": {  # Feature not in SDK!
            "aspect_ratio": "16:9"
        }
    }
}

# Make authenticated request
access_token = self._get_access_token(credentials)
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

api_endpoint = f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:generateContent"

response = requests.post(api_endpoint, headers=headers, json=payload, timeout=120)
response.raise_for_status()
response_data = response.json()

# Parse JSON response (handle both camelCase and snake_case)
candidates = response_data.get("candidates", [])
for cand in candidates:
    parts_list = cand.get("content", {}).get("parts", [])
    for part in parts_list:
        if "inlineData" in part or "inline_data" in part:
            inline_data = part.get("inlineData") or part.get("inline_data", {})
            mime = inline_data.get("mimeType") or inline_data.get("mime_type")
            data_b64 = inline_data.get("data", "")
            data_bytes = base64.b64decode(data_b64)
```

**Key Considerations**:

1. **Dependencies**: Use `google-auth` instead of full SDK (`google-cloud-aiplatform`, `google-genai`)
1. **Regional Availability**: Some models only work in specific regions (e.g., `us-central1`), not `global`
1. **Model Names**: Check for `-preview` suffix differences between preview and stable models
1. **Authentication Scopes**: Use `https://www.googleapis.com/auth/cloud-platform` for Vertex AI
1. **Response Format**: Handle both camelCase (API) and snake_case (some SDKs) field names
1. **Base64 Encoding**: REST API expects base64-encoded strings for binary data
1. **Error Handling**: Parse JSON error responses for detailed error messages

**Trade-offs**:

- ✅ Immediate access to all API features
- ✅ Lighter dependencies
- ✅ Full control over requests
- ❌ More implementation work
- ❌ Must handle auth/tokens manually
- ❌ Need to track API changes yourself

### Complex Type Management Systems

For nodes that need sophisticated type negotiation between multiple parameters:

```python
class IfElse(BaseNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # Sophisticated connection tracking for type management
        self._possibility_space: list[str] = []  # Types acceptable to output target
        self._locked_type: str | None = None     # Specific type locked by input
        self._connected_inputs: set[str] = set() # Track input connections
        self._output_connected: bool = False     # Track output connections

    def _update_parameter_types(self) -> None:
        """Update all parameter types based on current state."""
        if self._locked_type:
            # Locked to specific type - everything uses that type
            self.output_if_true.input_types = [self._locked_type]
            self.output_if_false.input_types = [self._locked_type]
            self.output.output_type = self._locked_type
        elif self._possibility_space:
            # Flexible within possibility space
            self.output_if_true.input_types = self._possibility_space.copy()
            self.output_if_false.input_types = self._possibility_space.copy()
            self.output.output_type = ParameterTypeBuiltin.ALL.value
        else:
            # Default state - accept any type
            self.output_if_true.input_types = ["any"]
            self.output_if_false.input_types = ["any"]
            self.output.output_type = ParameterTypeBuiltin.ALL.value
```

**Best Practice**: Use sophisticated type management for nodes that route data between multiple inputs and outputs.

### Agentic Nodes

Inherit from ControlNode for agent management:

```python
from griptape.structures import Agent
from griptape_nodes.exe_types.node_types import ControlNode

class MyAgentNode(ControlNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_parameter(Parameter(
            name="agent_in",
            input_types=["Agent"],
            type="Agent"
        ))
        self.add_parameter(Parameter(
            name="agent_out",
            output_type="Agent"
        ))

    def process(self) -> None:
        agent_state = self.get_parameter_value("agent_in")
        agent = Agent.from_dict(agent_state) if agent_state else Agent()
        # Process with agent
        self.parameter_output_values["agent_out"] = agent.to_dict()
```

### Abstract Base Classes for Node Families

Create abstract base classes for related nodes to share common functionality:

```python
from abc import abstractmethod
from typing import Any

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode

class BaseCustomIterativeStartNode(BaseIterativeStartNode):
    """Base class for a family of custom iterative start nodes."""

    @abstractmethod
    def _get_compatible_end_classes(self) -> set[type]:
        """Return the set of End node classes that this Start node can connect to."""

    @abstractmethod
    def _get_parameter_group_name(self) -> str:
        """Return the name for the parameter group containing iteration data."""

    @abstractmethod
    def _get_exec_out_display_name(self) -> str:
        """Return the display name for the exec_out parameter."""

    @abstractmethod
    def _get_exec_out_tooltip(self) -> str:
        """Return the tooltip for the exec_out parameter."""

    @abstractmethod
    def _get_iteration_items(self) -> list[Any]:
        """Get the list of items to iterate over."""

    @abstractmethod
    def is_loop_finished(self) -> bool:
        """Return True if the loop has completed all iterations."""
```

**Best Practice**: Use abstract base classes to share common logic across node families while enforcing implementation of specific methods.

### Caching

Use ClassVar for shared resources:

```python
from typing import ClassVar, Any

class CachedModelNode(DataNode):
    _cache: ClassVar[dict[str, Any]] = {}

    def get_model(self, model_id: str) -> Any:
        if model_id not in self._cache:
            self._cache[model_id] = load_model(model_id)
        return self._cache[model_id]
```

### Hub Integration (e.g., HuggingFace)

```python
# Gated model detection
is_gated = getattr(model, 'gated', False)
model_dict['gated'] = is_gated

# Status updates for gated models
if getattr(model_info, 'gated', False):
    self.publish_update_to_parameter(
        "status",
        "🔒 GATED MODEL - May require approval"
    )
```

### ParameterMessage for External Links and Status Updates

```python
from griptape_nodes.exe_types.core_types import ParameterMessage

# External link example
ParameterMessage(
    name="model_card_link",
    title="Model Card",
    variant="info",
    value="View model documentation",
    button_link=f"https://huggingface.co/{model_id}",
    button_text="View on HuggingFace"
)

# Dynamic status message example
class MyIterativeNode(BaseIterativeStartNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # Status message parameter for real-time updates
        self.status_message = ParameterMessage(
            name="status_message",
            variant="info",
            value="",
        )
        self.add_node_element(self.status_message)

    def _update_status_message(self, status_type: str = "normal") -> None:
        """Update status message based on current state."""
        if self._total_iterations == 0:
            status = "Completed 0 (of 0)"
        elif status_type == "break":
            status = f"Stopped at {self._current_iteration_count} (of {self._total_iterations}) - Break"
        elif self.is_loop_finished():
            status = f"Completed {self._total_iterations} (of {self._total_iterations})"
        else:
            status = f"Processing {self._current_iteration_count} (of {self._total_iterations})"

        self.status_message.value = status
```

**Best Practice**: Use ParameterMessage for static external links, dynamic status updates, and deprecation notices. For a full walkthrough of the deprecation notice pattern (hidden message + `before_value_set` auto-migration), see [Deprecated Model Migration and User Notification](execution_and_lifecycle.md#deprecated-model-migration-and-user-notification).

## Modern UI/UX Patterns

### Advanced UI Options

```python
ui_options={
    "hide_property": True,      # Hide from property panel
    "pulse_on_run": True,       # Visual feedback during execution
    "expander": True,           # Collapsible parameter groups
    "is_full_width": True,      # Full-width display
    "multiline": True,          # Multi-line text input
    "placeholder_text": "...",  # Input placeholder
    "display_name": "...",      # Custom display name
    "markdown": True,           # Markdown rendering
    "compare": True,            # Comparison mode
    "clickable_file_browser": True,  # File browser integration
    "hide": True,               # ⭐ CORRECT: Completely hide parameter
    "collapsed": True,          # Start parameter groups collapsed
    "edit_mask": True,          # Enable mask editing for images
}
```

#### Hidden Parameters Best Practice

Use `"hide": True` to hide parameters from the UI (for advanced/expert settings):

```python
# ✅ CORRECT: Hidden parameter with slider
num_images_param = Parameter(
    name="num_images",
    input_types=["int"],
    type="int",
    default_value=1,
    tooltip="Number of images to generate (1-9)",
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    ui_options={
        "display_name": "Number of Images",
        "hide": True  # ⭐ CORRECT pattern
    },
)
num_images_param.add_trait(Slider(min_val=1, max_val=9))
self.add_parameter(num_images_param)

# ⚠️ LEGACY: "hidden": True exists but is rare (3 instances vs 47)
ui_options={"hidden": True}  # Less common, prefer "hide": True
```

**Common Use Cases for Hidden Parameters:**

- Advanced/expert configuration options
- Internal control signals
- Debug parameters
- Optional advanced features
- Parameters that should only be set programmatically

### Success/Failure Node Pattern

For nodes that can succeed or fail, inherit from SuccessFailureNode:

```python
from griptape_nodes.exe_types.node_types import SuccessFailureNode

class LoadImage(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Add status parameters using the helper method
        self._create_status_parameters(
            result_details_tooltip="Details about the image loading operation result",
            result_details_placeholder="Details on the load attempt will be presented here.",
        )

    def process(self) -> None:
        # Reset execution state at start
        self._clear_execution_status()

        # Clear output values to prevent stale data on errors
        self.parameter_output_values["image"] = None

        try:
            # Processing logic here
            result = load_image()
            self.parameter_output_values["image"] = result

            # Success case
            success_details = f"Image loaded successfully from {source}"
            self._set_status_results(was_successful=True, result_details=f"SUCCESS: {success_details}")

        except Exception as e:
            error_details = f"Failed to load image: {e}"
            self._set_status_results(was_successful=False, result_details=f"FAILURE: {error_details}")
            self._handle_failure_exception(e)
```

**Best Practice**: Use SuccessFailureNode for operations that can fail and need to report status to users.

### Parameter Initialization

Initialize parameter visibility on node creation:

```python
def _initialize_parameter_visibility(self) -> None:
    """Initialize parameter visibility based on default values."""
    default_model = self.get_parameter_value("model") or "default"
    if default_model == "text-only":
        self.hide_parameter_by_name("image_input")
    else:
        self.show_parameter_by_name("image_input")
```

### Artifact Path Tethering Pattern

For nodes that work with files, use the artifact tethering pattern to keep path and artifact parameters synchronized:

```python
from griptape_nodes_library.utils.artifact_path_tethering import (
    ArtifactPathTethering,
    ArtifactTetheringConfig,
)

class LoadImage(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Configuration for artifact tethering
        self._tethering_config = ArtifactTetheringConfig(
            dict_to_artifact_func=dict_to_image_url_artifact,
            extract_url_func=self._extract_url_from_image_value,
            supported_extensions=self.SUPPORTED_EXTENSIONS,
            default_extension="png",
            url_content_type_prefix="image/",
        )

        # Create artifact parameter
        self.image_parameter = Parameter(
            name="image",
            input_types=["ImageUrlArtifact", "ImageArtifact", "str"],
            type="ImageUrlArtifact",
            output_type="ImageUrlArtifact",
            ui_options={"clickable_file_browser": True},
        )

        # Create path parameter using tethering utility
        self.path_parameter = ArtifactPathTethering.create_path_parameter(
            name="path",
            config=self._tethering_config,
            display_name="File Path or URL",
        )

        # Tethering helper keeps parameters in sync
        self._tethering = ArtifactPathTethering(
            node=self,
            artifact_parameter=self.image_parameter,
            path_parameter=self.path_parameter,
            config=self._tethering_config,
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        # Delegate tethering logic to helper
        self._tethering.on_after_value_set(parameter, value)
        return super().after_value_set(parameter, value)
```

**Best Practice**: Use artifact tethering for seamless file/URL parameter synchronization.

## Two-Mode UI Pattern (Simple + Custom)

**Use Case**: Create beginner-friendly nodes while offering advanced control for power users.

**Example**: Music/video generation APIs often have "simple description" mode and "detailed control" mode.

### Implementation Pattern

```python
class GenerativeNode(DataNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Mode selector
        mode_param = Parameter(
            name="custom_mode",
            input_types=["bool"],
            type="bool",
            default_value=False,
            tooltip="Custom Mode: Full control. Simple Mode: Auto-generate from prompt.",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"display_name": "Custom Mode"},
        )
        self.add_parameter(mode_param)

        # Prompt (meaning changes by mode)
        prompt_param = Parameter(
            name="prompt",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip=[
                {"type": "text", "text": "Custom Mode: Exact lyrics/script"},
                {"type": "text", "text": "Simple Mode: General description"},
            ],
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"multiline": True, "display_name": "Prompt"},
        )
        self.add_parameter(prompt_param)

        # Advanced parameters (custom mode only)
        style_param = Parameter(
            name="style",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip="Style/genre (Custom Mode only)",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"hide": True},  # Hidden by default
        )
        self.add_parameter(style_param)

        title_param = Parameter(
            name="title",
            input_types=["str"],
            type="str",
            default_value="",
            tooltip="Title (Custom Mode only)",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            ui_options={"hide": True},  # Hidden by default
        )
        self.add_parameter(title_param)

        # Initialize visibility
        self._initialize_parameter_visibility()

    def _initialize_parameter_visibility(self) -> None:
        """Initialize parameter visibility based on default mode."""
        custom_mode = self.get_parameter_value("custom_mode") or False
        if custom_mode:
            self.show_parameter_by_name("style")
            self.show_parameter_by_name("title")
        else:
            self.hide_parameter_by_name("style")
            self.hide_parameter_by_name("title")

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Update UI based on mode selection."""
        if parameter.name == "custom_mode":
            if value:
                self.show_parameter_by_name("style")
                self.show_parameter_by_name("title")
            else:
                self.hide_parameter_by_name("style")
                self.hide_parameter_by_name("title")

        return super().after_value_set(parameter, value)

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate based on selected mode."""
        exceptions = []
        custom_mode = self.get_parameter_value("custom_mode")

        if custom_mode:
            # Custom mode requires style and title
            style = self.get_parameter_value("style") or ""
            title = self.get_parameter_value("title") or ""

            if not style.strip():
                exceptions.append(ValueError(f"{self.name}: Style required in Custom Mode"))
            if not title.strip():
                exceptions.append(ValueError(f"{self.name}: Title required in Custom Mode"))
        else:
            # Simple mode just needs prompt
            prompt = self.get_parameter_value("prompt") or ""
            if not prompt.strip():
                exceptions.append(ValueError(f"{self.name}: Prompt required in Simple Mode"))

        return exceptions if exceptions else None
```

**Best Practice for First-Time Users**: Default to Simple Mode with recommendation in documentation:

```markdown
### Getting Started

#### Simple Mode (Recommended for First-Time Users)

1. Leave "Custom Mode" unchecked
2. Enter a description: "A calm piano melody"
3. Run!

#### Custom Mode (Advanced)

1. Check "Custom Mode"
2. Fill in style, title, and detailed prompt
3. Fine-tune advanced parameters
```

## Music/Audio Generation API Patterns

### Character Limits by Model

Many generation APIs have model-specific character limits. Store limits as class constants:

```python
class MusicGenerationNode(DataNode):
    # Prompt length limits by model
    PROMPT_LIMITS_CUSTOM = {
        "V3_5": 3000,
        "V4": 3000,
        "V4_5": 5000,
        "V5": 5000,
    }
    PROMPT_LIMIT_SIMPLE = 500

    # Style length limits by model
    STYLE_LIMITS = {
        "V3_5": 200,
        "V4": 200,
        "V4_5": 1000,
        "V5": 1000,
    }

    TITLE_LIMIT = 80

    def validate_before_node_run(self) -> list[Exception] | None:
        """Validate with model-specific limits."""
        exceptions = []
        model = self.get_parameter_value("model")
        custom_mode = self.get_parameter_value("custom_mode")

        if custom_mode:
            prompt = self.get_parameter_value("prompt") or ""
            prompt_limit = self.PROMPT_LIMITS_CUSTOM.get(model, 3000)
            if len(prompt) > prompt_limit:
                exceptions.append(ValueError(
                    f"{self.name}: Prompt exceeds {prompt_limit} character limit for {model} "
                    f"(current: {len(prompt)} characters)"
                ))

            style = self.get_parameter_value("style") or ""
            style_limit = self.STYLE_LIMITS.get(model, 200)
            if len(style) > style_limit:
                exceptions.append(ValueError(
                    f"{self.name}: Style exceeds {style_limit} character limit for {model} "
                    f"(current: {len(style)} characters)"
                ))

        return exceptions if exceptions else None
```

### Model Selection with Detailed Tooltips

Use list of dict tooltip format for model comparison:

```python
model_param = Parameter(
    name="model",
    input_types=["str"],
    type="str",
    default_value="V5",
    tooltip=[
        {"type": "text", "text": "Model version for generation:"},
        {"type": "text", "text": "• V5: Superior quality, fastest (4 min max)"},
        {"type": "text", "text": "• V4_5PLUS: Richest sound, up to 8 min"},
        {"type": "text", "text": "• V4_5: Superior blending, up to 8 min"},
        {"type": "text", "text": "• V4: Best quality, refined structure (4 min)"},
        {"type": "text", "text": "• V3_5: Creative diversity (4 min)"},
    ],
    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
)
model_param.add_trait(Options(choices=["V5", "V4_5PLUS", "V4_5", "V4", "V3_5"]))
```

### Dual Track Output Pattern

APIs that generate multiple variations:

```python
# Output parameter for multiple tracks
music_urls_param = Parameter(
    name="music_urls",
    output_type="list[str]",
    type="list[str]",
    tooltip="Download URLs for generated tracks (2 variations)",
    allowed_modes={ParameterMode.OUTPUT},
    settable=False,
    ui_options={"is_full_width": True, "display_name": "Music URLs"},
)
self.add_parameter(music_urls_param)

def process(self) -> None:
    # ... generation logic ...
    urls = self._extract_music_urls(response_data)
    self.parameter_output_values["music_urls"] = urls

    # Build detailed result
    result_lines = [
        f"✓ Generated {len(urls)} track variation(s)",
        "",
        "Music URLs:",
    ]
    for i, url in enumerate(urls, 1):
        result_lines.append(f"{i}. {url}")

    self.parameter_output_values["result_details"] = "\n".join(result_lines)
```

### Status Updates During Long Operations

Update status parameter in real-time during polling:

```python
def _poll_for_completion(self, task_id: str, api_key: str) -> dict[str, Any]:
    """Poll API with real-time status updates."""
    for attempt in range(self.MAX_POLLING_ATTEMPTS):
        time.sleep(self.POLLING_INTERVAL)

        # Update status parameter with progress
        status_msg = f"Generating... ({attempt + 1}/{self.MAX_POLLING_ATTEMPTS})"
        self.set_parameter_value("status", status_msg)

        response = requests.get(query_url, headers=headers, params={"ids": task_id})
        # ... check completion ...
```

**Best Practice**: Always provide progress feedback for operations longer than 10 seconds.

## Flexible Artifact Processing

### Duck Typing for Artifacts

Handle multiple artifact formats gracefully:

```python
def _extract_image_value(self, image_input: Any) -> str | None:
    """Extract string value from various image input types."""
    if isinstance(image_input, str):
        return image_input

    try:
        # ImageUrlArtifact: .value holds URL string
        if hasattr(image_input, "value"):
            value = getattr(image_input, "value", None)
            if isinstance(value, str):
                return value

        # ImageArtifact: .base64 holds raw or data-URI
        if hasattr(image_input, "base64"):
            b64 = getattr(image_input, "base64", None)
            if isinstance(b64, str) and b64:
                return b64
    except Exception as e:
        self._log(f"Failed to extract image value: {e}")

    return None
```

### Image Format Conversion for External APIs

**Problem**: External APIs often have strict format requirements (e.g., JPEG, PNG, WebP only), but cameras may save images in unsupported formats like MPO (Multi Picture Object) for 3D/burst photos.

**Solution**: Automatically detect and convert unsupported formats:

```python
# Import at top of file
from PIL import Image
from io import BytesIO

def _get_image_data(self, image_artifact: ImageArtifact | ImageUrlArtifact) -> str:
    """Convert image to API-compatible format."""
    # ... extract image_bytes ...

    try:
        img = Image.open(BytesIO(image_bytes))

        # Convert unsupported formats (MPO, TIFF, BMP, etc.) to JPEG
        if img.format not in ['JPEG', 'PNG', 'WEBP']:
            self._log(f"Converting {img.format} to JPEG for API compatibility")
            # Convert to RGB if needed (for formats like MPO)
            if img.mode not in ['RGB', 'L']:
                img = img.convert('RGB')
            # Save as JPEG to bytes
            output = BytesIO()
            img.save(output, format='JPEG', quality=95)
            image_bytes = output.getvalue()
            mime_type = "image/jpeg"
        else:
            format_to_mime = {
                'JPEG': 'image/jpeg',
                'PNG': 'image/png',
                'WEBP': 'image/webp'
            }
            mime_type = format_to_mime.get(img.format, 'image/jpeg')
    except Exception as e:
        self._log(f"Could not detect image format: {e}")
        mime_type = "image/jpeg"

    # Encode as base64 data URI
    base64_data = base64.b64encode(image_bytes).decode('utf-8')
    return f"data:{mime_type};base64,{base64_data}"
```

**Key Points**:

- Apply conversion at all image input points (ImageArtifact, ImageUrlArtifact, localhost URLs)
- Use high quality (95%) to preserve image fidelity
- Handle color mode conversion (MPO often uses non-RGB modes)
- Log conversions for debugging
- Gracefully fall back to JPEG if detection fails

### Utility Function Patterns

Create reusable utility functions for common operations:

```python
# Connection checking utilities
def _outgoing_connection_exists(source_node: str, source_param: str) -> bool:
    """Check if a source node/parameter has any outgoing connections."""
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()
    source_connections = connections.outgoing_index.get(source_node)
    if source_connections is None:
        return False

    param_connections = source_connections.get(source_param)
    return bool(param_connections) if param_connections else False

def _incoming_connection_exists(target_node: str, target_param: str) -> bool:
    """Check if a target node/parameter has any incoming connections."""
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()
    target_connections = connections.incoming_index.get(target_node)
    if target_connections is None:
        return False

    param_connections = target_connections.get(target_param)
    return bool(param_connections) if param_connections else False
```

**Best Practice**: Create utility functions for common operations like connection checking, validation, and data processing.

### Flexible Image Processing

```python
def _image_to_bytes(self, image_artifact) -> bytes:
    """Convert various image artifact types to bytes."""
    if not image_artifact:
        raise ValueError("No input image provided")

    try:
        # Handle dictionary format (serialized artifacts)
        if isinstance(image_artifact, dict):
            image_url_artifact = self._dict_to_image_url_artifact(image_artifact)
            image_bytes = image_url_artifact.to_bytes()
        # Handle artifact objects directly
        elif isinstance(image_artifact, (ImageArtifact, ImageUrlArtifact)):
            image_bytes = image_artifact.to_bytes()
        else:
            # Try generic to_bytes method
            image_bytes = image_artifact.to_bytes()

        # Verify we have valid image data
        if not image_bytes or len(image_bytes) < 100:
            raise ValueError("Image data is empty or too small")

        return image_bytes

    except Exception as e:
        raise ValueError(f"Failed to extract image data: {str(e)}")
```

## Appendix

### Imports

```python
# Core imports
from griptape_nodes.exe_types.core_types import (
    Parameter, ParameterList, ParameterMode, ParameterTypeBuiltin,
    ParameterGroup, ParameterMessage, ControlParameterInput, ControlParameterOutput
)
from griptape_nodes.exe_types.node_types import (
    DataNode, ControlNode, BaseNode, SuccessFailureNode,
    StartNode, EndNode
)
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode, BaseIterativeEndNode
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.color_picker import ColorPicker
from griptape_nodes.traits.file_system_picker import FileSystemPicker

# Artifacts
from griptape.artifacts import ImageArtifact, ImageUrlArtifact, TextArtifact

# Utilities
from griptape_nodes_library.utils.artifact_path_tethering import (
    ArtifactPathTethering, ArtifactTetheringConfig
)
from griptape_nodes_library.utils.image_utils import (
    dict_to_image_url_artifact,
    load_pil_from_url,
    save_pil_image_with_named_filename,
)
from griptape_nodes_library.utils.file_utils import generate_filename
```

### Utility Function Reference

#### Image Utilities (`griptape_nodes_library.utils.image_utils`)

| Function                                            | Purpose                                              | Returns            |
| --------------------------------------------------- | ---------------------------------------------------- | ------------------ |
| `dict_to_image_url_artifact(d)`                     | Convert dict representation to ImageUrlArtifact      | `ImageUrlArtifact` |
| `load_pil_from_url(url)`                            | Load PIL Image from URL (handles localhost)          | `PIL.Image.Image`  |
| `save_pil_image_with_named_filename(img, filename)` | Save PIL Image using project system (see note below) | `ImageUrlArtifact` |

**Example usage:**

```python
from griptape_nodes_library.utils.image_utils import (
    dict_to_image_url_artifact,
    load_pil_from_url,
    save_pil_image_with_named_filename,
)

# Convert parameter value to artifact
value = self.get_parameter_value("image")
if isinstance(value, dict):
    artifact = dict_to_image_url_artifact(value)
else:
    artifact = value

# Load as PIL for processing
pil_image = load_pil_from_url(artifact.value)

# Process image...
processed = pil_image.filter(...)

# Save and get output artifact
output_artifact = save_pil_image_with_named_filename(processed, "result.png")
self.parameter_output_values["output"] = output_artifact
```

#### File Utilities (`griptape_nodes_library.utils.file_utils`)

| Function                                    | Purpose                    | Returns |
| ------------------------------------------- | -------------------------- | ------- |
| `generate_filename(node_name, suffix, ext)` | Create consistent filename | `str`   |

**Example usage:**

```python
from griptape_nodes_library.utils.file_utils import generate_filename

# Generate filename like "ColorMatch_processed_abc123.png"
filename = generate_filename(self.name, suffix="processed", ext="png")
```

#### Project System (`griptape_nodes.files.project_file`)

| Class/Function                            | Purpose                                          | Returns                  |
| ----------------------------------------- | ------------------------------------------------ | ------------------------ |
| `ProjectFileDestination.from_situation()` | Create file destination with named situation     | `ProjectFileDestination` |
| `ProjectFileParameter`                    | Parameter component for configurable file output | -                        |

**Example usage:**

```python
from griptape_nodes.files.project_file import ProjectFileDestination

# Save file using project system
dest = ProjectFileDestination.from_situation(
    filename="output.mp4",
    situation="save_node_output"
)
saved = dest.write_bytes(file_bytes)
artifact = VideoUrlArtifact(saved.location)
```

**Note:** The utility functions `save_pil_image_with_named_filename()` and similar helpers use the project system internally. For new code, prefer using `ProjectFileParameter` or `ProjectFileDestination` directly to have full control over file handling. See [Working with the Project System](project_system.md) for comprehensive documentation.

### Advanced Parameter Types

- **ControlParameterInput/Output**: For execution flow control
- **ParameterGroup**: For organizing related parameters with collapsible UI
- **ParameterMessage**: For status updates and external links
- **ParameterList**: For accepting multiple inputs of the same type

### Enumerations

- **NodeResolutionState**: UNRESOLVED, RESOLVING, RESOLVED
- **ParameterMode**: INPUT, OUTPUT, PROPERTY
- **ParameterTypeBuiltin**: STR("str"), BOOL("bool"), INT("int"), FLOAT("float"), ANY("any"), NONE("none"), CONTROL_TYPE("parametercontroltype"), ALL("all")

### Advanced Node Types

- **BaseNode**: Most basic node type for custom implementations
- **DataNode**: For data processing without execution flow
- **ControlNode**: For nodes that manage execution flow
- **SuccessFailureNode**: For operations that can succeed or fail
- **BaseIterativeStartNode/BaseIterativeEndNode**: For iterative operations
- **AsyncResult**: Generator type for yielding blocking work to a background thread (prefer overriding `async def aprocess()` for genuinely async work)

### Custom Artifacts

Inherit from BaseArtifact and override methods as needed:

```python
from griptape.artifacts import BaseArtifact

class CustomArtifact(BaseArtifact):
    def __init__(self, value: Any, **kwargs):
        super().__init__(value, **kwargs)

    def to_text(self) -> str:
        return str(self.value)
```

### Advanced Lifecycle Methods

#### Spotlight Control

For conditional dependency resolution:

```python
def initialize_spotlight(self) -> None:
    """Custom spotlight initialization - only include evaluate parameter initially."""
    evaluate_param = self.get_parameter_by_name("evaluate")
    if evaluate_param and ParameterMode.INPUT in evaluate_param.get_mode():
        self.current_spotlight_parameter = evaluate_param

def advance_parameter(self) -> bool:
    """Custom parameter advancement with conditional dependency resolution."""
    if self.current_spotlight_parameter is None:
        return False

    # Special handling for conditional parameters
    if self.current_spotlight_parameter is self.evaluate:
        try:
            evaluation_result = self.check_evaluation()
            next_param = self.output_if_true if evaluation_result else self.output_if_false

            if ParameterMode.INPUT in next_param.get_mode():
                self.current_spotlight_parameter.next = next_param
                next_param.prev = self.current_spotlight_parameter
                self.current_spotlight_parameter = next_param
                return True
        except Exception:
            self.current_spotlight_parameter = None
            return False

    return super().advance_parameter()
```

#### Control Flow Management

```python
def get_next_control_output(self) -> Parameter | None:
    """Return the appropriate control output based on evaluation."""
    if "evaluate" not in self.parameter_output_values:
        self.stop_flow = True
        return None

    if self.parameter_output_values["evaluate"]:
        return self.get_parameter_by_name("Then")
    return self.get_parameter_by_name("Else")
```
