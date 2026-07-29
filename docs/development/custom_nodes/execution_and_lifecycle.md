# Execution and Lifecycle

This page covers the callbacks a node can override to participate in the workflow lifecycle, and the patterns for long-running asynchronous work such as external API integration with polling.

## Lifecycle Callbacks

All callbacks are overridable:

- **allow_incoming_connection**, **allow_outgoing_connection**: Return bool for connection validation
- **after_incoming_connection**, **after_outgoing_connection**: Handle post-connection logic
- **after_incoming_connection_removed**, **after_outgoing_connection_removed**: Handle disconnection
- **before_value_set**: Return modified value before setting
- **after_value_set**: React to parameter value changes
- **validate_before_workflow_run**, **validate_before_node_run**: Return list[Exception]|None
- **on_griptape_event**: Handle workflow events
- **initialize_spotlight**: Setup spotlight functionality
- **get_next_control_output**: Return Parameter|None for control flow

### Helper Methods

- `hide_parameter_by_name()`, `show_parameter_by_name()`
- `append_value_to_parameter()`
- `publish_update_to_parameter()`
- `show_message_by_name()`, `hide_message_by_name()`, `get_message_by_name_or_element_id()`

## Asynchronous API Integration

Nodes have two ways to perform long-running work without stalling the engine:

1. **Override `async def aprocess()` (preferred).** The engine awaits it on its event loop, so genuinely asynchronous integrations (async HTTP clients, `await asyncio.sleep()` polling) run concurrently with the rest of the engine.
1. **Override `process()` and `yield` callables (`AsyncResult`).** Each yielded callable is executed **synchronously on a background thread** — the engine stays responsive, but the work itself is still blocking, sequential code.

If you are writing a new integration, reach for `aprocess()` and async I/O. Use the `yield` pattern when your integration is built on synchronous libraries (`requests`, blocking SDKs) that you don't want to rewrite.

### Async Processing with `aprocess()` (Preferred)

```python
import asyncio

import httpx
from griptape_nodes.exe_types.node_types import ControlNode

POLLING_INTERVAL = 10  # seconds (use API-recommended value)
MAX_POLLING_ATTEMPTS = 60  # 10 minutes max

class MyAsyncNode(ControlNode):
    async def aprocess(self) -> None:
        """Process the request asynchronously."""
        try:
            # Set safe defaults
            self._set_safe_defaults()

            # Validate API key
            api_key = self._validate_api_key()

            async with httpx.AsyncClient(timeout=60) as client:
                # Submit task
                task_id = await self._submit_task(client, api_key)

                # Poll for completion
                result = await self._poll_for_completion(client, task_id, api_key)

            # Process result
            self.parameter_output_values["output"] = result

        except Exception as e:
            self._set_safe_defaults()
            self._log(f"Processing failed: {e}")
            raise RuntimeError(f"{self.name}: {e}") from e

    async def _submit_task(self, client: httpx.AsyncClient, api_key: str) -> str:
        response = await client.post(
            "https://api.example.com/v1/tasks",
            json=self._build_payload(),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.json()["task_id"]

    async def _poll_for_completion(self, client: httpx.AsyncClient, task_id: str, api_key: str) -> str:
        for attempt in range(MAX_POLLING_ATTEMPTS):
            await asyncio.sleep(POLLING_INTERVAL)  # never time.sleep() in aprocess

            response = await client.get(
                "https://api.example.com/v1/query/task",
                params={"task_id": task_id},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            status_data = response.json()

            if status_data["status"] == "Success":
                return status_data["result"]
            if status_data["status"] == "Fail":
                error_msg = status_data.get("error_message", "Unknown error")
                raise RuntimeError(f"Task failed: {error_msg}")
            # Continue polling for "Processing", "Pending", etc.

        raise RuntimeError(f"Task did not complete within {MAX_POLLING_ATTEMPTS * POLLING_INTERVAL} seconds")
```

**Key Points:**

- Override `async def aprocess()` instead of `process()` — the engine awaits it directly
- Use async I/O throughout: `httpx.AsyncClient` for requests, `await asyncio.sleep()` for polling delays
- Blocking calls (`requests`, `time.sleep()`) inside `aprocess()` stall the engine's event loop — if you must call a blocking function, wrap it with `await asyncio.to_thread(blocking_fn)`
- The base class's default `aprocess()` wraps `process()`, so nodes only need to override one of the two

### Blocking Work on a Background Thread (`process()` + `yield`)

For integrations built on synchronous libraries, override `process()` and yield a callable. The engine runs each yielded callable synchronously on a background thread and resumes the generator with its return value — the engine stays responsive, but this does **not** make the work itself asynchronous:

```python
from griptape_nodes.exe_types.node_types import ControlNode, AsyncResult

class MyBlockingNode(ControlNode):
    def process(self) -> AsyncResult | None:
        """Yield the blocking work to a background thread."""
        yield lambda: self._process()

    def _process(self) -> None:
        """Main processing method (runs synchronously on a background thread)."""
        try:
            # Set safe defaults
            self._set_safe_defaults()

            # Validate API key
            api_key = self._validate_api_key()

            # Submit task
            task_id = self._submit_task(api_key)

            # Poll for completion
            result = self._poll_for_completion(task_id, api_key)

            # Process result
            self.parameter_output_values["output"] = result

        except Exception as e:
            self._set_safe_defaults()
            self._log(f"Processing failed: {e}")
            raise RuntimeError(f"{self.name}: {str(e)}") from e
```

**Key Points:**

- `process()` returns `AsyncResult | None` and yields a callable
- Each yielded callable runs synchronously on a background thread; the generator resumes with its return value
- Fine for existing synchronous code (`requests`, blocking SDKs), but prefer `aprocess()` for new integrations

### Polling Pattern for Long-Running Tasks

When integrating with APIs that use asynchronous task processing (video generation, model training, etc.), implement a three-step pattern. The examples below use synchronous `requests` calls, suited to the background-thread pattern above; in `aprocess()`, use `httpx.AsyncClient` and `await asyncio.sleep()` instead, as shown earlier.

#### Step 1: Task Submission

```python
def _submit_task(self, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Submit task and return response with task_id."""
    payload = self._build_payload(params)

    response = requests.post(
        self.API_BASE_URL,
        json=payload,
        headers=headers,
        timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()

    response_data = response.json()
    task_id = response_data.get("task_id")
    return response_data
```

#### Step 2: Status Polling

```python
POLLING_INTERVAL = 10  # seconds (use API-recommended value)
MAX_POLLING_ATTEMPTS = 60  # 10 minutes max

def _poll_for_completion(self, task_id: str, headers: dict[str, str]) -> str | None:
    """Poll API for task completion and return result identifier."""
    query_url = "https://api.example.com/v1/query/task"

    for attempt in range(MAX_POLLING_ATTEMPTS):
        time.sleep(POLLING_INTERVAL)  # Wait before each poll

        response = requests.get(
            query_url,
            headers=headers,
            params={"task_id": task_id},  # Use query params, not path
            timeout=DEFAULT_TIMEOUT
        )
        response.raise_for_status()

        status_data = response.json()
        status = status_data.get("status")

        self._log(f"Polling attempt {attempt + 1}: Status = {status}")

        if status == "Success":
            file_id = status_data.get("file_id")
            return file_id
        elif status == "Fail":
            error_msg = status_data.get("error_message", "Unknown error")
            raise RuntimeError(f"Task failed: {error_msg}")
        # Continue polling for "Processing", "Pending", etc.

    raise RuntimeError(f"Task did not complete within {MAX_POLLING_ATTEMPTS * POLLING_INTERVAL} seconds")
```

#### Step 3: Result Retrieval

```python
def _retrieve_result(self, file_id: str, headers: dict[str, str]) -> str:
    """Retrieve download URL from result identifier."""
    retrieve_url = "https://api.example.com/v1/files/retrieve"

    response = requests.get(
        retrieve_url,
        headers=headers,
        params={"file_id": file_id},
        timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()

    response_data = response.json()
    download_url = response_data.get("file", {}).get("download_url")

    return download_url
```

**Key Considerations:**

- Always use API-recommended polling intervals (typically 5-10 seconds)
- Set reasonable maximum attempts to prevent infinite loops
- Use query parameters, not path parameters, for task_id (verify with API docs)
- Handle all status states: Success, Fail, Processing, Pending
- Log polling attempts for debugging
- Set safe defaults on failure

### Dynamic Endpoint Selection Based on Inputs

When a node can operate in multiple modes depending on which inputs are connected (e.g., image-to-video when images are provided, text-to-video when they are not), select the API endpoint dynamically in the process method rather than hardcoding a single URL:

```python
IMAGE2VIDEO_URL = "https://api.example.com/v1/videos/image2video"
TEXT2VIDEO_URL = "https://api.example.com/v1/videos/text2video"

def _process(self):
    image_data = self._get_image_data("start_frame")
    has_images = image_data is not None

    if has_images:
        api_url = IMAGE2VIDEO_URL
    else:
        api_url = TEXT2VIDEO_URL

    payload = self._build_payload()
    if image_data:
        payload["image"] = image_data

    response = requests.post(api_url, headers=headers, json=payload, timeout=30)
    # ... polling uses the same api_url for status checks
    poll_url = f"{api_url}/{task_id}"
```

This avoids requiring image inputs when the user wants text-only generation, and ensures the correct API endpoint is called for each mode. The polling URL should use the same base endpoint.

### Image Artifact Conversion to Base64

**CRITICAL: Localhost URL Handling**

When sending images to external APIs, ImageUrlArtifact URLs from static storage are localhost and inaccessible to external services. Always detect and convert localhost URLs to base64:

```python
import base64

def _get_image_data(self, image_artifact: ImageArtifact | ImageUrlArtifact) -> str:
    """Convert image artifact to URL or base64 data URI."""

    # ImageUrlArtifact - check if localhost or public URL
    if isinstance(image_artifact, ImageUrlArtifact):
        url = image_artifact.value

        # Localhost URLs must be converted to base64 for external APIs
        if url.startswith(('http://localhost', 'http://127.0.0.1',
                          'https://localhost', 'https://127.0.0.1')):
            self._log(f"Converting localhost URL to base64: {url[:100]}...")
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            image_bytes = response.content

            # Detect MIME type from headers
            mime_type = response.headers.get('content-type', 'image/jpeg')
            if not mime_type.startswith('image/'):
                mime_type = 'image/jpeg'

            base64_data = base64.b64encode(image_bytes).decode('utf-8')
            return f"data:{mime_type};base64,{base64_data}"

        # Public URLs can be passed through
        self._log(f"Using public URL: {url[:100]}...")
        return url

    # ImageArtifact - use .base64 property (preferred method)
    if isinstance(image_artifact, ImageArtifact):
        # PREFERRED: Use built-in properties
        if hasattr(image_artifact, 'base64') and hasattr(image_artifact, 'mime_type'):
            base64_data = image_artifact.base64  # Raw base64 (no prefix)
            mime_type = image_artifact.mime_type  # e.g., 'image/jpeg'

            # Check if already has data URI prefix
            if base64_data.startswith('data:'):
                self._log("Using ImageArtifact.base64 (already has data URI)")
                return base64_data

            # Add data URI prefix
            self._log(f"Using ImageArtifact.base64 with mime_type: {mime_type}")
            return f"data:{mime_type};base64,{base64_data}"

        # FALLBACK: Manual byte extraction
        self._log("Falling back to manual base64 encoding")
        if hasattr(image_artifact, 'value') and hasattr(image_artifact.value, 'read'):
            image_artifact.value.seek(0)
            image_bytes = image_artifact.value.read()
        elif hasattr(image_artifact, 'data'):
            if isinstance(image_artifact.data, bytes):
                image_bytes = image_artifact.data
            elif hasattr(image_artifact.data, 'read'):
                image_artifact.data.seek(0)
                image_bytes = image_artifact.data.read()
            else:
                raise ValueError("Unsupported ImageArtifact format")
        else:
            raise ValueError("Unsupported ImageArtifact format")

        # Detect MIME type with PIL
        mime_type = "image/jpeg"
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))
            format_to_mime = {
                'JPEG': 'image/jpeg',
                'PNG': 'image/png',
                'WEBP': 'image/webp'
            }
            mime_type = format_to_mime.get(img.format, 'image/jpeg')
        except Exception:
            pass

        base64_data = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:{mime_type};base64,{base64_data}"

    raise ValueError("Unsupported artifact type")
```

**Key Points:**

1. **Always detect localhost URLs** - External APIs cannot access them
1. **Use ImageArtifact.base64 property** - The proper Griptape way (returns raw base64)
1. **Use ImageArtifact.mime_type property** - Automatic MIME type detection
1. **Log which path is used** - Essential for debugging
1. **Download localhost files** - Convert to base64 before sending to API

**Parameter Definition:**

```python
Parameter(
    name="image_input",
    input_types=["ImageArtifact", "ImageUrlArtifact"],  # Accept both
    type="ImageArtifact",
    tooltip="Image input (file or URL)",
    ui_options={"clickable_file_browser": True},  # Enable file browser
)
```

### Multi-Image Input Validation

When nodes accept multiple image parameters, use a reusable validation method with clear parameter identification:

```python
def _validate_image(self, image_artifact: ImageArtifact | ImageUrlArtifact,
                    param_name: str) -> list[Exception]:
    """Validate image with parameter name in error messages."""
    exceptions = []

    if isinstance(image_artifact, ImageArtifact):
        # Get image bytes
        if hasattr(image_artifact, 'value') and hasattr(image_artifact.value, 'read'):
            image_artifact.value.seek(0)
            image_bytes = image_artifact.value.read()
            image_artifact.value.seek(0)
        else:
            return exceptions

        # Validate size
        size_mb = len(image_bytes) / (1024 * 1024)
        if size_mb >= 20:
            exceptions.append(ValueError(
                f"{self.name}: {param_name} size must be < 20MB (current: {size_mb:.1f}MB)"
            ))

        # Validate format and dimensions
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))

            if img.format not in ['JPEG', 'PNG', 'WEBP']:
                exceptions.append(ValueError(
                    f"{self.name}: {param_name} format must be JPG, PNG, or WebP (current: {img.format})"
                ))

            width, height = img.size
            short_edge = min(width, height)
            if short_edge <= 300:
                exceptions.append(ValueError(
                    f"{self.name}: {param_name} short edge must be > 300px (current: {short_edge}px)"
                ))
        except ImportError:
            self._log("PIL not available for validation")
        except Exception as e:
            self._log(f"Error validating {param_name}: {e}")

    return exceptions

def validate_before_node_run(self) -> list[Exception] | None:
    """Validate all image parameters."""
    exceptions = []

    # Validate each image parameter independently
    first_frame = self.get_parameter_value("first_frame_image")
    if first_frame:
        exceptions.extend(self._validate_image(first_frame, "first_frame_image"))

    last_frame = self.get_parameter_value("last_frame_image")
    if last_frame:
        exceptions.extend(self._validate_image(last_frame, "last_frame_image"))

    return exceptions if exceptions else None
```

**Benefits:**

- Clear error messages identifying which image parameter has issues
- Reusable validation logic across multiple image inputs
- Independent validation for each parameter
- Actionable feedback for users

### Model-Dependent Parameter Management

When different models support different parameter combinations:

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    """Handle model-dependent parameter visibility and options."""
    if parameter.name == "model":
        if value == "AdvancedModel":
            # Show model-specific parameters
            self.show_parameter_by_name("advanced_option")

            # Update dropdown choices dynamically
            resolution_param = self.get_parameter_by_name("resolution")
            if resolution_param:
                for child in resolution_param.children:
                    if hasattr(child, 'choices'):
                        child.choices = ADVANCED_MODEL_RESOLUTIONS
                        break
        else:
            # Hide and reset for other models
            self.hide_parameter_by_name("advanced_option")

            # Update to standard choices
            resolution_param = self.get_parameter_by_name("resolution")
            if resolution_param:
                for child in resolution_param.children:
                    if hasattr(child, 'choices'):
                        child.choices = STANDARD_RESOLUTIONS
                        break
                self.set_parameter_value("resolution", "720P")

    return super().after_value_set(parameter, value)
```

**Model-Specific Validation:**

```python
def validate_before_node_run(self) -> list[Exception] | None:
    """Validate model-specific parameter combinations."""
    exceptions = []

    model = self.get_parameter_value("model")
    duration = self.get_parameter_value("duration")
    resolution = self.get_parameter_value("resolution")

    # Example: 10s only for specific model/resolution
    if duration == 10:
        if model != "AdvancedModel":
            exceptions.append(ValueError(f"{self.name}: 10s duration only supported by AdvancedModel"))
        elif resolution == "4K":
            exceptions.append(ValueError(f"{self.name}: 10s duration not supported with 4K resolution"))

    # Model-specific parameter requirements
    if model in ["ModelB", "ModelC"]:
        required_param = self.get_parameter_value("required_for_model_b_c")
        if not required_param:
            exceptions.append(ValueError(f"{self.name}: Parameter required for {model}"))

    return exceptions if exceptions else None
```

### Deprecated Model Migration and User Notification

When a model provider deprecates endpoints (e.g., preview models replaced by GA equivalents), nodes should automatically migrate saved workflows while informing the user. This pattern uses three components working together:

1. A `DEPRECATED_MODELS` dictionary mapping old model names to their replacements
1. A hidden `ParameterMessage` element that acts as a dismissable info banner
1. The `before_value_set` lifecycle hook to intercept and replace deprecated values before they are applied

**Step 1: Define the deprecation map and current models**

```python
from griptape_nodes.exe_types.core_types import Parameter, ParameterMessage
from griptape_nodes.traits.button import Button

MODELS = [
    "veo-3.1-generate-001",
    "veo-3.1-fast-generate-001",
]

# Mapping of deprecated model names to their replacements.
# When a saved workflow references one of these, the node auto-migrates.
DEPRECATED_MODELS: dict[str, str] = {
    "veo-3.1-generate-preview": "veo-3.1-generate-001",
    "veo-3.1-fast-generate-preview": "veo-3.1-fast-generate-001",
    "veo-3.0-generate-001": "veo-3.1-generate-001",
    "veo-2.0-generate-001": "veo-3.1-generate-001",
}
```

**Step 2: Add a hidden ParameterMessage in `__init__`**

Place this after the model parameter so it appears near the model selector in the UI. The `hide=True` keeps it invisible until needed. The `Button` trait with `on_click` gives the user a "Dismiss" button.

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # ... model parameter added above ...

    # Hidden deprecation notice — shown when a deprecated model is detected
    self.add_node_element(
        ParameterMessage(
            name="model_deprecation_notice",
            title="Model Deprecation Notice",
            variant="info",
            value="",
            traits={
                Button(
                    full_width=True,
                    on_click=lambda _, __: self.hide_message_by_name("model_deprecation_notice"),
                )
            },
            button_text="Dismiss",
            hide=True,
        )
    )
```

**Step 3: Implement `before_value_set` to intercept deprecated models**

`before_value_set` fires before the parameter's value is applied. This is the right place to swap a deprecated model for its replacement, because `after_value_set` (and any logic that depends on the model value) will see the replacement.

```python
def before_value_set(self, parameter: Parameter, value: Any) -> Any:
    """Auto-migrate deprecated models and show a deprecation notice."""
    if parameter.name == "model" and value in DEPRECATED_MODELS:
        replacement = DEPRECATED_MODELS[value]
        message = self.get_message_by_name_or_element_id("model_deprecation_notice")
        if message is not None:
            message.value = (
                f"The '{value}' model has been deprecated. "
                f"The model has been updated to '{replacement}'. "
                "Please save your workflow to apply this change."
            )
            self.show_message_by_name("model_deprecation_notice")
        value = replacement

    return super().before_value_set(parameter, value)
```

**Step 4: Hide the notice when the user selects a valid model**

In `after_value_set`, dismiss the banner when the current model is not deprecated. This handles the case where the user manually selects a different model after the migration.

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    if parameter.name == "model":
        # ... model-specific logic (update duration choices, etc.) ...
        if value not in DEPRECATED_MODELS:
            self.hide_message_by_name("model_deprecation_notice")

    return super().after_value_set(parameter, value)
```

**How it works end-to-end:**

1. A user opens a workflow saved with `"veo-3.1-generate-preview"`.
1. The framework calls `before_value_set` with the saved value.
1. The hook detects it in `DEPRECATED_MODELS`, swaps it to `"veo-3.1-generate-001"`, and shows the info banner.
1. `after_value_set` fires with the replacement value — model-dependent UI updates (duration choices, parameter visibility, etc.) work correctly because they see the valid GA model.
1. The user sees the banner: *"The 'veo-3.1-generate-preview' model has been deprecated. The model has been updated to 'veo-3.1-generate-001'. Please save your workflow to apply this change."*
1. The user can dismiss the banner or it hides automatically on the next valid model selection.

**Key API methods used:**

| Method                                         | Purpose                                           |
| ---------------------------------------------- | ------------------------------------------------- |
| `self.add_node_element(ParameterMessage(...))` | Adds the message element to the node              |
| `self.get_message_by_name_or_element_id(name)` | Retrieves the message element to update its value |
| `self.show_message_by_name(name)`              | Makes the hidden message visible                  |
| `self.hide_message_by_name(name)`              | Hides the message again                           |

**Reference implementations:**

- `GriptapeCloudPrompt` in `griptape_nodes_library/config/prompt/griptape_cloud_prompt.py` (standard library)
- `VeoVideoGenerator`, `VeoImageToVideoGenerator`, `VeoTextToVideoWithRef` in the `griptape-nodes-library-googleai` external library

### Enhanced Debug Logging for API Integration

For nodes that integrate with external APIs, implement comprehensive debug logging to quickly diagnose issues:

```python
# Task Submission - Log full response
def _submit_task(self, params: dict, headers: dict) -> dict:
    response = requests.post(API_URL, json=payload, headers=headers)
    response.raise_for_status()

    response_data = response.json()
    self._log(f"Task submission response: {json.dumps(response_data, indent=2)}")
    return response_data

# Payload Sizes - Log data sizes before sending
def _log_request(self, payload: dict) -> None:
    if "first_frame_image" in payload:
        img_len = len(payload.get("first_frame_image", ""))
        self._log(f"first_frame_image data length: {img_len} chars (~{img_len/1024:.1f}KB)")

    if "last_frame_image" in payload:
        img_len = len(payload.get("last_frame_image", ""))
        self._log(f"last_frame_image data length: {img_len} chars (~{img_len/1024:.1f}KB)")

# Error Responses - Log full API error details
def _poll_for_completion(self, task_id: str, headers: dict) -> str:
    status_data = response.json()
    status = status_data.get("status")

    if status == "Fail":
        # Log complete error response for debugging
        self._log(f"Full API error response: {json.dumps(status_data, indent=2)}")
        error_msg = status_data.get("error_message", "Unknown error")
        raise RuntimeError(f"Task failed: {error_msg}")

# Processing Paths - Log which code path is executed
def _get_image_data(self, image_artifact) -> str:
    if isinstance(image_artifact, ImageUrlArtifact):
        if url.startswith('http://localhost'):
            self._log(f"Converting localhost URL to base64: {url[:100]}...")
        else:
            self._log(f"Using public URL: {url[:100]}...")
    elif isinstance(image_artifact, ImageArtifact):
        if hasattr(image_artifact, 'base64'):
            self._log(f"Using ImageArtifact.base64 with mime_type: {mime_type}")
        else:
            self._log("Falling back to manual base64 encoding")
```

**What to Log:**

- **Full API responses** (submission, polling, retrieval)
- **Payload sizes** (especially for base64 data)
- **Processing paths** (which code branches execute)
- **Model/parameter combinations** being used
- **Error details** (full error response from API)

**Benefits:**

- Quickly identify where failures occur
- Understand what data is being sent
- Track which code paths execute
- Get exact API error messages and codes
- Debug without reproducing issues

### API Documentation Verification

**Critical Best Practice:** Always verify API specifications directly from documentation.

**Common Pitfalls to Avoid:**

1. **Model Names**: Check exact capitalization (`MiniMax-Hailuo-02` not `video-01`)
1. **Endpoints**: Verify exact URLs (`/v1/query/video_generation` not `/v1/video_generation/{id}`)
1. **Parameters**: Check query params vs path params
1. **Response Structure**: Verify exact field names (`file_id` vs `file_list`)
1. **Polling Intervals**: Use API-recommended values

**Example: Correct vs Incorrect Polling:**

```python
# ✅ CORRECT: Query parameter
response = requests.get(
    "https://api.example.com/v1/query/task",
    params={"task_id": task_id}
)

# ❌ INCORRECT: Path parameter (unless API specifies this)
response = requests.get(
    f"https://api.example.com/v1/query/task/{task_id}"
)
```

**When Documentation is Inaccessible:**

- Explicitly state inability to access web pages (e.g., JavaScript-heavy docs)
- Request user to provide relevant documentation sections
- Never assume or infer API patterns without verification
- Update implementation when code samples are provided
