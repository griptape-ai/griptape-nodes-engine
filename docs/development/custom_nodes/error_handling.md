# Best Practices and Error Handling

General best practices for production-quality nodes: secrets, imports, code quality, error handling, validation, and logging.

## Best Practices

### Core Principles

- **Descriptive names and tooltips**
- **Robust error handling with validators**
- **Single responsibility per node**
- **Use `SecretsManager` for API keys and secrets**
- **Import all dependencies at module level**
- **Idempotent process methods**

### Secrets Management

Use `GriptapeNodes.SecretsManager()` to access API keys and secrets:

```python
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

class MyNode(DataNode):
    SERVICE_NAME = "MyService"
    API_KEY_NAME = "MY_SERVICE_API_KEY"

    def _validate_api_key(self) -> str:
        api_key = GriptapeNodes.SecretsManager().get_secret(self.API_KEY_NAME)
        if not api_key:
            raise ValueError(f"Missing {self.API_KEY_NAME}")
        return api_key
```

**Key Points:**

- Import `GriptapeNodes` at module level, not inside functions
- Use `SecretsManager().get_secret()` to retrieve secrets
- Define `API_KEY_NAME` as a class constant for consistency
- Always validate that the secret exists before using it

### Import Best Practices

**Always import dependencies at module level, not inside functions:**

❌ **Bad** - Conditional/lazy imports:

```python
def _get_image_data(self, image_artifact):
    try:
        from PIL import Image  # Don't do this
        from io import BytesIO
        img = Image.open(BytesIO(image_bytes))
```

✅ **Good** - Module-level imports:

```python
# At top of file
from PIL import Image
from io import BytesIO

def _get_image_data(self, image_artifact):
    img = Image.open(BytesIO(image_bytes))
```

**Why?**

- Makes dependencies clear and visible
- Avoids redundant imports throughout the file
- Follows Python best practices (PEP 8)
- Easier to catch missing dependencies early
- Better IDE support and code completion

**Exception**: Only use conditional imports for truly optional dependencies that may not be installed:

```python
def process(self) -> None:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        error_msg = "huggingface_hub library not installed"
        self.parameter_output_values["output"] = None
        raise ImportError(error_msg)
```

### Import Organization

Organize imports in standard order with blank lines between groups:

```python
# Standard library imports
import base64
import logging
from typing import Any

# Third-party imports
import requests
from PIL import Image

# Local/Griptape imports
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
```

### Type Checking for Third-Party Libraries

When importing third-party libraries, you may encounter type checking errors. Use the appropriate `type: ignore` comment based on the situation:

#### Scenario 1: Library Installed but Missing Type Stubs

For libraries that are installed but lack type annotations (like `sklearn`, `ultralytics`, `supervision`):

```python
# ✅ Library exists but has no type stubs
from sklearn.cluster import KMeans  # type: ignore[import-untyped]
from ultralytics import YOLO  # type: ignore[import-untyped]
from supervision import Detections  # type: ignore[import-untyped]
```

#### Scenario 2: Library Not Installed in CI Type Checking Environment

For libraries that are runtime dependencies but not installed in the CI type checking environment (like `color-matcher`, specialized processing libraries):

```python
# ✅ Library not installed in type checking environment
from color_matcher import ColorMatcher  # type: ignore[reportMissingImports]
from color_matcher.normalizations import norm_img_to_uint8  # type: ignore[reportMissingImports]
```

#### When to Use Which

| Error Type             | Comment                                | Use When                         |
| ---------------------- | -------------------------------------- | -------------------------------- |
| `import-untyped`       | `# type: ignore[import-untyped]`       | Library installed, no type stubs |
| `reportMissingImports` | `# type: ignore[reportMissingImports]` | Library not in CI environment    |

**General guidance:**

- `import-untyped` is preferred when both work - it's more precise
- `reportMissingImports` is necessary when the library isn't available during type checking
- Check CI logs to determine which error you're actually getting

### Function Parameter Management

Keep function argument counts low (under 6) by using dataclasses:

❌ **Bad** - Too many parameters:

```python
def process_bbox(self, x: int, y: int, width: int, height: int,
                 dilation_percent: float, img_width: int, img_height: int):
    # Process bounding box
```

✅ **Good** - Use dataclass:

```python
from dataclasses import dataclass

@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int
    dilation_percent: float
    img_width: int
    img_height: int

def process_bbox(self, bbox: BoundingBox):
    # Process bounding box using bbox.x, bbox.y, etc.
```

**Benefits:**

- Improved readability
- Type safety
- Easier to maintain
- Self-documenting code

### Code Quality

**Additional linting best practices:**

- Remove trailing whitespace from all lines (including blank lines)
- Use consistent indentation (spaces only, no tabs)
- Keep lines under 120 characters when possible
- Use descriptive variable names
- Avoid adding unnecessary Python packaging scaffolding. Create `__init__.py` files only when you actually want a package (or need them for your chosen packaging approach).

#### Pre-commit checks (required)

Before committing in `griptape-nodes`, run formatting and checks and fix any errors:

```bash
make format
make check/lint
make check/types
```

#### Node docs + navigation

When adding a new node to the core library, also add node reference documentation:

- Create a docs page at: `docs/nodes/<category>/<node>.md`
- Add it to `mkdocs.yml` under: `nav -> Nodes Reference -> <Category>`

#### Common gotchas

- Repo-wide lint/type checks can surface issues in **untracked** files too. Avoid leaving untracked folders/files in the repo (for example, copied scratch folders) when running checks or preparing a PR.
- If ruff flags function complexity (e.g., `C901`, `PLR0912`), prefer refactoring into smaller helpers over suppressing.
- **`parent_container_name` ≠ `parent_element_name`**: These two `Parameter` attributes look similar but serve completely different purposes. `parent_container_name` is for `ParameterContainer` (list/dictionary ownership), `parent_element_name` is for `ParameterGroup` (UI grouping). Mixing them up causes parameters to land at the node root, skip cleanup between runs, and silently vanish on save/reload. See the [Containers](parameters.md#containers) section for the full distinction.

## Production Error Handling

### Comprehensive Validation

Use `validate_before_node_run()` for complex validation:

```python
def validate_before_node_run(self) -> list[Exception] | None:
    """Validate parameters before running the node."""
    exceptions = []

    model = self.get_parameter_value("model")
    if model == "advanced":
        images = self.get_parameter_list_value("images") or []
        if len(images) > MAX_IMAGES:
            exceptions.append(ValueError(
                f"{self.name}: Maximum {MAX_IMAGES} images allowed, got {len(images)}"
            ))

    return exceptions if exceptions else None
```

### Connection Validation Patterns

For complex nodes with multiple connection requirements:

```python
def _validate_iterative_connections(self) -> list[Exception]:
    """Validate that all required connections are properly established."""
    errors = []
    node_type = self._get_base_node_type_name()

    # Check if exec_out has outgoing connections
    if not _outgoing_connection_exists(self.name, self.exec_out.name):
        errors.append(
            Exception(
                f"{self.name}: Missing required connection from 'On Each Item'. "
                f"REQUIRED ACTION: Connect {node_type} Start to interior loop nodes. "
                "The start node must connect to other nodes to execute the loop body."
            )
        )

    # Check if loop has outgoing connection to End
    if self.end_node is None:
        errors.append(
            Exception(
                f"{self.name}: Missing required tethering connection. "
                f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Node' to {node_type} End 'Loop Start Node'. "
                "This establishes the explicit relationship between start and end nodes."
            )
        )

    return errors
```

**Best Practice**: Provide detailed, actionable error messages that tell users exactly what connections are missing and how to fix them.

### Safe Defaults Pattern

Always set safe defaults before raising exceptions:

```python
def _set_safe_defaults(self) -> None:
    """Set safe default values for all outputs."""
    self.parameter_output_values["result"] = None
    self.parameter_output_values["status"] = "error"
    self.parameter_output_values["count"] = 0

def process(self) -> None:
    try:
        # Processing logic
        result = process_data()
        self.parameter_output_values["result"] = result
    except Exception as e:
        self._set_safe_defaults()
        raise RuntimeError(f"Processing failed: {str(e)}") from e
```

### URL Construction

Use `urllib.parse.urljoin()` for safe URL building:

```python
from urllib.parse import urljoin
import os

def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # Safe URL construction
    base = os.getenv("API_BASE_URL", "https://api.example.com")
    base_slash = base if base.endswith("/") else base + "/"
    api_base = urljoin(base_slash, "api/")
    self._endpoint = urljoin(api_base, "v1/process/")
```

## Logging Best Practices

### Safe Logging Pattern

Prevent logging failures from breaking execution:

```python
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)

def _log(self, message: str) -> None:
    """Safe logging with exception suppression."""
    with suppress(Exception):
        logger.info(message)
```

### Request Sanitization

Sanitize sensitive data in logs:

```python
from copy import deepcopy
import json

PROMPT_TRUNCATE_LENGTH = 100

def _log_request(self, payload: dict[str, Any]) -> None:
    """Log request with sanitized sensitive data."""
    with suppress(Exception):
        sanitized_payload = deepcopy(payload)

        # Truncate long prompts
        prompt = sanitized_payload.get("prompt", "")
        if len(prompt) > PROMPT_TRUNCATE_LENGTH:
            sanitized_payload["prompt"] = prompt[:PROMPT_TRUNCATE_LENGTH] + "..."

        # Redact base64 image data
        if "image" in sanitized_payload:
            image_data = sanitized_payload["image"]
            if isinstance(image_data, str) and image_data.startswith("data:image/"):
                parts = image_data.split(",", 1)
                header = parts[0] if parts else "data:image/"
                b64_len = len(parts[1]) if len(parts) > 1 else 0
                sanitized_payload["image"] = f"{header},<base64 data length={b64_len}>"

        self._log(f"Request: {json.dumps(sanitized_payload, indent=2)}")
```
