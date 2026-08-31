"""API Key Provider parameter component for reusable API key switching functionality."""

from typing import Any, NamedTuple

from griptape_nodes.exe_types.core_types import Parameter, ParameterMessage
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.retained_mode.events.secrets_events import (
    GetSecretValueRequest,
    GetSecretValueResultSuccess,
)
from griptape_nodes.traits.button import Button


class ApiKeyValidationResult(NamedTuple):
    """Result from API key validation.

    Attributes:
        proxy_api_key: The Griptape Cloud API key for Authorization header (always required)
        user_api_key: Optional user-provided API key for X-GTC-PROXY-AUTH-API-KEY header (None if not enabled)
    """

    proxy_api_key: str
    user_api_key: str | None


class ApiKeyProviderParameter:
    """Reusable component for adding API key provider switching functionality to nodes.

    This component adds:
    - A toggle parameter to enable user-provided API key (always uses proxy API)
    - A button on the toggle to open secrets settings
    - A message that shows/hides based on whether the user API key is set
    - Helper methods to validate and retrieve API keys

    Example usage:
        ```python
        api_key_provider = ApiKeyProviderParameter(
            node=self,
            api_key_name="BFL_API_KEY",
            provider_name="BlackForest Labs",
            api_key_url="https://dashboard.bfl.ai/api/keys",
        )
        api_key_provider.add_parameters()

        # In your node's _process method:
        validation_result = api_key_provider.validate_api_key()
        headers = {
            "Authorization": f"Bearer {validation_result.proxy_api_key}",
            "Content-Type": "application/json",
        }
        if validation_result.user_api_key:
            headers["X-GTC-PROXY-AUTH-API-KEY"] = validation_result.user_api_key
        ```

    Args:
        node: The BaseNode instance to add parameters to
        api_key_name: The name of the user's API key secret (e.g., "BFL_API_KEY")
        provider_name: The display name of the API provider (e.g., "BlackForest Labs")
        api_key_url: The URL where users can obtain their API key
        parameter_name: Optional name for the toggle parameter (default: "api_key_provider")
        proxy_api_key_name: Optional name for the proxy API key (default: "GT_CLOUD_API_KEY")
        on_label: Label when user API is enabled (default: "Customer")
        off_label: Label when proxy API is enabled (default: "Griptape")
    """

    def __init__(  # noqa: PLR0913
        self,
        node: BaseNode,
        api_key_name: str,
        provider_name: str,
        api_key_url: str,
        *,
        parameter_name: str = "api_key_provider",
        proxy_api_key_name: str = "GT_CLOUD_API_KEY",
        on_label: str = "Customer",
        off_label: str = "Griptape",
    ) -> None:
        self._node = node
        self.api_key_name = api_key_name
        self.provider_name = provider_name
        self.api_key_url = api_key_url
        self.parameter_name = parameter_name
        self.proxy_api_key_name = proxy_api_key_name
        self.on_label = on_label
        self.off_label = off_label
        self.message_name = f"{parameter_name}_message"

    def add_parameters(self) -> None:
        """Add the API key provider toggle and message to the node."""
        self._node.add_parameter(
            ParameterBool(
                name=self.parameter_name,
                default_value=False,
                tooltip="Use customer API key instead of the default one",
                allow_input=False,
                allow_output=False,
                on_label=self.on_label,
                off_label=self.off_label,
                ui_options={"display_name": "API Key Provider"},
                traits={
                    Button(
                        icon="key",
                        tooltip="Open secrets settings",
                        button_link=f"#settings-secrets?filter={self.api_key_name}",
                    )
                },
            )
        )
        self._node.add_node_element(
            ParameterMessage(
                name=self.message_name,
                variant="info",
                title=f"{self.provider_name} API key",
                value=(
                    f"To use your own {self.provider_name} API key, visit:\n{self.api_key_url}\n"
                    f"to obtain a valid key.\n\n"
                    f"Then set {self.api_key_name} in "
                    f"[Settings → Secrets](#settings-secrets?filter={self.api_key_name})."
                ),
                button_link=f"#settings-secrets?filter={self.api_key_name}",
                button_text="Open Secrets",
                button_icon="key",
                markdown=True,
                hide=True,
            )
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Handle visibility of the API key message when the provider toggle changes."""
        if parameter.name != self.parameter_name:
            return

        if value:
            api_key_value = self.check_api_key_set(self.api_key_name)
            if not api_key_value:
                self._node.show_message_by_name(self.message_name)
        else:
            self._node.hide_message_by_name(self.message_name)

    def is_user_api_enabled(self) -> bool:
        """Check if user API key is currently enabled.

        Returns:
            bool: True if user API key is enabled, False otherwise
        """
        return bool(self._node.get_parameter_value(self.parameter_name) or False)

    def check_api_key_set(self, api_key: str) -> bool:
        """Check if an API key exists and is not empty.

        Args:
            api_key: The name of the API key secret to check

        Returns:
            bool: True if the API key exists and is not empty, False otherwise
        """
        api_key_value = self._get_secret(api_key, should_error_on_not_found=False)
        if api_key_value is None:
            return False
        if isinstance(api_key_value, str):
            return bool(api_key_value.strip())
        return bool(api_key_value)

    def get_api_key(self, *, use_user_api: bool) -> str:
        """Get the API key for the specified mode.

        Args:
            use_user_api: True to get user API key, False to get proxy API key

        Returns:
            str: The API key value

        Raises:
            ValueError: If the API key is not set
        """
        api_key_name = self.api_key_name if use_user_api else self.proxy_api_key_name
        api_key = self._get_secret(api_key_name, should_error_on_not_found=True)
        if not api_key:
            msg = f"{self._node.name} is missing {api_key_name}. Ensure it's set in the environment/config."
            raise ValueError(msg)
        return api_key

    def validate_api_key(self) -> ApiKeyValidationResult:
        """Validate and return API keys for proxy API usage.

        Always returns proxy API key. Optionally returns user API key if enabled.

        Returns:
            ApiKeyValidationResult: Named tuple containing proxy_api_key and optional user_api_key

        Raises:
            ValueError: If the proxy API key is not set, or if user API is enabled but user key is not set
        """
        # Always need proxy API key
        proxy_api_key = self.get_api_key(use_user_api=False)

        # Optionally get user API key if enabled
        user_api_key = None
        if self.is_user_api_enabled():
            user_api_key = self.get_api_key(use_user_api=True)

        return ApiKeyValidationResult(proxy_api_key=proxy_api_key, user_api_key=user_api_key)

    def _get_secret(self, secret_name: str, *, should_error_on_not_found: bool) -> str | None:
        """Ask the engine for a secret.

        Asked as a request rather than read from a local SecretsManager: when the node runs in
        a worker, the request reaches the orchestrator, while a manager read would answer from
        the worker's own copy.
        """
        result = self._node.engine.handle_request(
            GetSecretValueRequest(key=secret_name, should_error_on_not_found=should_error_on_not_found)
        )
        if not isinstance(result, GetSecretValueResultSuccess):
            return None
        return result.value
