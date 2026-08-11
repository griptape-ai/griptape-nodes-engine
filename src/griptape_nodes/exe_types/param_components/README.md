# Using ApiKeyProviderParameter Component

The `ApiKeyProviderParameter` component provides a reusable way to add user-provided API key support to nodes that use the Griptape Cloud proxy API. When users provide their own API key, it's automatically forwarded to the provider via the `X-GTC-PROXY-AUTH-API-KEY` header.

## Overview

This component automatically adds:

- A toggle parameter to enable user-provided API key (always uses proxy API)
- A button on the toggle to open secrets settings (filtered to the relevant API key)
- An informational message that shows/hides based on whether the user API key is set
- Helper methods to validate and retrieve API keys

**Key Simplification:** All requests go through the Griptape Cloud proxy API. When a user provides their own API key, it's simply forwarded via the `X-GTC-PROXY-AUTH-API-KEY` header - no need to switch between different API endpoints or configurations!

## Example: Flux Image Generation Node

The `FluxImageGeneration` node demonstrates a complete implementation. Here's how it works:

### Step 1: Import the Component

Add this import at the top of your node file:

```python
from griptape_nodes.exe_types.param_components.api_key_provider_parameter import ApiKeyProviderParameter
```

**File location:** Add this to your imports section (usually near the top with other imports)

### Step 2: Define API Key Configuration Constants

Define class-level constants for your API key information. These will be used when initializing the component:

```python
class YourNode(SuccessFailureNode):
    # API key configuration
    USER_API_KEY_NAME = "YOUR_API_KEY_NAME"  # e.g., "BFL_API_KEY", "OPENAI_API_KEY"
    USER_API_KEY_URL = "https://example.com/api/keys"  # URL where users can get their API key
    USER_API_KEY_PROVIDER_NAME = "Your Provider Name"  # e.g., "BlackForest Labs", "OpenAI"
```

**File location:** Add these as class attributes, typically near the top of your class definition (after `SERVICE_NAME` and `API_KEY_NAME` if you have them)

### Step 3: Initialize the Component in `__init__`

In your node's `__init__` method, create and initialize the component:

```python
def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    # ... your other initialization code ...
    
    # Add API key provider component
    self._api_key_provider = ApiKeyProviderParameter(
        node=self,
        api_key_name=self.USER_API_KEY_NAME,
        provider_name=self.USER_API_KEY_PROVIDER_NAME,
        api_key_url=self.USER_API_KEY_URL,
    )
    self._api_key_provider.add_parameters()
    
    # ... rest of your initialization ...
```

**File location:** Add this code in your `__init__` method, typically after any base class initialization and before adding other parameters

### Step 4: Override `after_value_set` Method

Add this method to handle visibility updates when the API key provider toggle changes:

```python
def after_value_set(self, parameter: Parameter, value: Any) -> None:
    self._api_key_provider.after_value_set(parameter, value)
    return super().after_value_set(parameter, value)
```

**File location:** Add this method to your node class, typically near other lifecycle methods like `_process` or `_validate_api_key`

### Step 5: Use the Component in Your Processing Logic

In your `_process` method (or wherever you need to get the API key), use the component's validation method:

```python
def _process(self) -> None:
    # ... your setup code ...
    
    try:
        validation_result = self._api_key_provider.validate_api_key()
    except ValueError as e:
        self._set_status_results(was_successful=False, result_details=str(e))
        self._handle_failure_exception(e)
        return
    
    # Build headers: always use proxy API key, optionally add user API key
    headers = {
        "Authorization": f"Bearer {validation_result.proxy_api_key}",
        "Content-Type": "application/json",
    }
    if validation_result.user_api_key:
        headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key
    
    # Use headers in your API calls - always goes through proxy API
    # ... rest of your processing ...
```

**File location:** Add this at the beginning of your `_process` method, before making any API calls

### Step 6: (Optional) Create a Helper Method

If you had an existing `_validate_api_key` method, you can simplify it to delegate to the component:

```python
def _validate_api_key(self) -> ApiKeyValidationResult:
    """Validate and return API keys for proxy API usage.
    
    Returns:
        ApiKeyValidationResult: Named tuple containing proxy_api_key and optional user_api_key
    """
    return self._api_key_provider.validate_api_key()
```

**File location:** Replace your existing `_validate_api_key` method with this, or add it if you don't have one. Don't forget to import `ApiKeyValidationResult`:

```python
from griptape_nodes.exe_types.param_components.api_key_provider_parameter import (
    ApiKeyProviderParameter,
    ApiKeyValidationResult,
)
```

## Complete Example

Here's a complete minimal example showing all the pieces together:

```python
from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.exe_types.param_components.api_key_provider_parameter import ApiKeyProviderParameter


class ExampleNode(SuccessFailureNode):
    """Example node with API key provider switching."""
    
    # API key configuration
    USER_API_KEY_NAME = "EXAMPLE_API_KEY"
    USER_API_KEY_URL = "https://example.com/api/keys"
    USER_API_KEY_PROVIDER_NAME = "Example Provider"
    
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.category = "API Nodes"
        self.description = "Example node with API key switching"
        
        # Add API key provider component
        self._api_key_provider = ApiKeyProviderParameter(
            node=self,
            api_key_name=self.USER_API_KEY_NAME,
            provider_name=self.USER_API_KEY_PROVIDER_NAME,
            api_key_url=self.USER_API_KEY_URL,
        )
        self._api_key_provider.add_parameters()
        
        # Add your other parameters here
        self.add_parameter(
            Parameter(
                name="input_text",
                type="str",
                tooltip="Input text",
            )
        )
    
    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        self._api_key_provider.after_value_set(parameter, value)
        return super().after_value_set(parameter, value)
    
    def _process(self) -> None:
        # Get API keys for proxy API usage
        try:
            validation_result = self._api_key_provider.validate_api_key()
        except ValueError as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
            return
        
        # Build headers: always use proxy API key, optionally add user API key
        headers = {
            "Authorization": f"Bearer {validation_result.proxy_api_key}",
            "Content-Type": "application/json",
        }
        if validation_result.user_api_key:
            headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key
        
        # Always use proxy API - user's key is forwarded via header if provided
        input_text = self.get_parameter_value("input_text")
        result = self._call_proxy_api(headers, input_text)
        
        # Process result...
        self._set_status_results(was_successful=True, result_details="Success")
    
    def _call_proxy_api(self, headers: dict[str, str], input_text: str) -> dict:
        # Your proxy API call logic here - always uses proxy endpoint
        # User's API key is automatically forwarded if provided
        pass
```

## Component API Reference

### Initialization Parameters

When creating an `ApiKeyProviderParameter` instance, you must provide:

- **`node`** (BaseNode): The node instance to add parameters to
- **`api_key_name`** (str): The name of the user's API key secret (e.g., `"BFL_API_KEY"`)
- **`provider_name`** (str): The display name of the API provider (e.g., `"BlackForest Labs"`)
- **`api_key_url`** (str): The URL where users can obtain their API key

Optional parameters:

- **`parameter_name`** (str, default: `"api_key_provider"`): Name for the toggle parameter
- **`proxy_api_key_name`** (str, default: `"GT_CLOUD_API_KEY"`): Name of the proxy API key secret
- **`on_label`** (str, default: `"Customer"`): Label when user API is enabled
- **`off_label`** (str, default: `"Griptape"`): Label when proxy API is enabled

### Available Methods

#### `add_parameters()`

Adds the toggle parameter and message to the node. Call this once in `__init__`.

#### `after_value_set(parameter: Parameter, value: Any)`

Handles visibility updates when the toggle changes. Call this from your node's `after_value_set` method.

#### `validate_api_key() -> ApiKeyValidationResult`

Validates and returns API keys for proxy API usage. Returns a `ApiKeyValidationResult` named tuple with:

- `proxy_api_key` (str): The Griptape Cloud API key for Authorization header (always required)
- `user_api_key` (str | None): Optional user-provided API key for X-GTC-PROXY-AUTH-API-KEY header (None if not enabled)

**Raises:** `ValueError` if the proxy API key is not set, or if user API is enabled but user key is not set.

#### `is_user_api_enabled() -> bool`

Checks if user API is currently enabled.

#### `get_api_key(use_user_api: bool) -> str`

Gets the API key for the specified mode.

**Raises:** `ValueError` if the API key is not set.

#### `check_api_key_set(api_key: str) -> bool`

Checks if an API key exists and is not empty.

## Migration Guide: Converting Existing Nodes

If you have an existing node that manually handles API key switching, here's how to migrate:

### Before (Manual Implementation)

```python
def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)

    # Manual toggle parameter
    self.add_parameter(
        ParameterBool(
            name="api_key_provider",
            default_value=False,
            # ... lots of configuration ...
        )
    )

    # Manual message
    self.add_node_element(
        ParameterMessage(
            name="set_api_key",
            # ... lots of configuration ...
        )
    )


def after_value_set(self, parameter: Parameter, value: Any) -> None:
    if parameter.name == "api_key_provider":
        # Manual visibility logic
        if value:
            if not self.check_api_key_set(self.USER_API_KEY_NAME):
                self.show_message_by_name("set_api_key")
        else:
            self.hide_message_by_name("set_api_key")
    return super().after_value_set(parameter, value)


def _validate_api_key(self) -> tuple[str, bool]:
    use_user_api = self.get_parameter_value("api_key_provider") or False
    api_key_name = "USER_KEY" if use_user_api else "GT_CLOUD_API_KEY"
    api_key = GriptapeNodes.SecretsManager().get_secret(api_key_name)
    # ... validation logic ...
    return api_key, use_user_api
```

### After (Using Component)

```python
from griptape_nodes.exe_types.param_components.api_key_provider_parameter import (
    ApiKeyProviderParameter,
    ApiKeyValidationResult,
)


def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)

    # Component handles everything
    self._api_key_provider = ApiKeyProviderParameter(
        node=self,
        api_key_name=self.USER_API_KEY_NAME,
        provider_name=self.USER_API_KEY_PROVIDER_NAME,
        api_key_url=self.USER_API_KEY_URL,
    )
    self._api_key_provider.add_parameters()


def after_value_set(self, parameter: Parameter, value: Any) -> None:
    self._api_key_provider.after_value_set(parameter, value)
    return super().after_value_set(parameter, value)


def _validate_api_key(self) -> ApiKeyValidationResult:
    return self._api_key_provider.validate_api_key()


def _process(self) -> None:
    validation_result = self._validate_api_key()

    # Build headers - always use proxy API
    headers = {
        "Authorization": f"Bearer {validation_result.proxy_api_key}",
        "Content-Type": "application/json",
    }
    if validation_result.user_api_key:
        headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key

    # Always use proxy endpoint
    # ... make API call with headers ...
```

## Common Patterns

### Pattern 1: Simple Proxy API Usage

All requests go through the proxy API. User's API key is automatically forwarded if provided:

```python
validation_result = self._api_key_provider.validate_api_key()

headers = {
    "Authorization": f"Bearer {validation_result.proxy_api_key}",
    "Content-Type": "application/json",
}
if validation_result.user_api_key:
    headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key

# Always use proxy endpoint
url = urljoin(self._proxy_base, "models/your-model")
response = await client.post(url, json=payload, headers=headers)
```

### Pattern 2: Logging User API Key Usage

You can log when a user-provided API key is being used:

```python
validation_result = self._api_key_provider.validate_api_key()

headers = {
    "Authorization": f"Bearer {validation_result.proxy_api_key}",
    "Content-Type": "application/json",
}
if validation_result.user_api_key:
    headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key
    self._log("Using user-provided API key via proxy")
else:
    self._log("Using default proxy API key")
```

## Troubleshooting

### Message Not Showing/Hiding

**Problem:** The message doesn't appear when the toggle is switched.

**Solution:** Make sure you're calling `self._api_key_provider.after_value_set(parameter, value)` in your node's `after_value_set` method and that it's called before `super().after_value_set()`.

### API Key Not Found Error

**Problem:** Getting `ValueError` about missing API key even though it's set.

**Solution:**

1. Verify the API key name matches exactly (case-sensitive)
1. Check that the secret is set in Settings → Secrets
1. Ensure you're using the correct `api_key_name` when initializing the component

### Toggle Not Appearing

**Problem:** The API key provider toggle doesn't appear in the UI.

**Solution:** Make sure you're calling `self._api_key_provider.add_parameters()` in your `__init__` method.

## See Also

- [Flux Image Generation Node](../../libraries/griptape_nodes_library/griptape_nodes_library/image/flux_image_generation.py) - Complete working example
- [ApiKeyProviderParameter Component](../../src/griptape_nodes/exe_types/param_components/api_key_provider_parameter.py) - Component source code
