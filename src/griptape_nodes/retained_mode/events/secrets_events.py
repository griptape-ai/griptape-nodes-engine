from dataclasses import dataclass
from typing import Any

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class GetSecretValueRequest(RequestPayload["GetSecretValueResultSuccess", "GetSecretValueResultFailure"]):
    """Get a secret value by key.

    Use when: Retrieving API keys, database credentials, authentication tokens,
    accessing sensitive configuration values, implementing secure storage.

    Args:
        key: Name of the secret key to retrieve
        should_error_on_not_found: Whether to error if the key is not found (default: True)

    Results: GetSecretValueResultSuccess (with value) | GetSecretValueResultFailure (key not found)
    """

    key: str
    should_error_on_not_found: bool = True


@dataclass
@PayloadRegistry.register
class GetSecretValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Secret value retrieved successfully.

    Args:
        value: The secret value (handle with care - avoid logging)
    """

    value: Any


@dataclass
@PayloadRegistry.register
class GetSecretValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Secret value retrieval failed. Common causes: key not found, access denied, secrets store unavailable."""


@dataclass
@PayloadRegistry.register
class SetSecretValueRequest(RequestPayload["SetSecretValueResultSuccess", "SetSecretValueResultFailure"]):
    """Set a secret value by key.

    Use when: Storing API keys, database credentials, authentication tokens,
    configuring secure settings, implementing secret management.

    Args:
        key: Name of the secret key to set
        value: Secret value to store (handle with care)

    Results: SetSecretValueResultSuccess | SetSecretValueResultFailure (storage error)
    """

    key: str
    value: Any


@dataclass
@PayloadRegistry.register
class SetSecretValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Secret value set successfully. Value is now stored securely."""


@dataclass
@PayloadRegistry.register
class SetSecretValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Secret value setting failed. Common causes: storage error, access denied, invalid key format."""


@dataclass
@PayloadRegistry.register
class GetAllSecretValuesRequest(RequestPayload["GetAllSecretValuesResultSuccess", "GetAllSecretValuesResultFailure"]):
    """Get all secret values.

    Use when: Backing up secrets, migrating configurations, implementing secret management UIs,
    debugging secret issues. Use with caution - returns all sensitive data.

    Results: GetAllSecretValuesResultSuccess (with values dict) | GetAllSecretValuesResultFailure (access error)
    """


@dataclass
@PayloadRegistry.register
class GetAllSecretValuesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """All secret values retrieved successfully.

    Args:
        values: Dictionary of all secret key-value pairs (handle with extreme care)
    """

    values: dict[str, Any]


@dataclass
@PayloadRegistry.register
class GetAllSecretValuesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Secret values retrieval failed. Common causes: access denied, secrets store unavailable."""


@dataclass
@PayloadRegistry.register
class DeleteSecretValueRequest(RequestPayload["DeleteSecretValueResultSuccess", "DeleteSecretValueResultFailure"]):
    """Delete a secret value by key.

    Use when: Removing obsolete secrets, cleaning up configurations, implementing secret rotation,
    revoking access credentials, managing secret lifecycle.

    Args:
        key: Name of the secret key to delete

    Results: DeleteSecretValueResultSuccess | DeleteSecretValueResultFailure (key not found, deletion error)
    """

    key: str


@dataclass
@PayloadRegistry.register
class DeleteSecretValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Secret value deleted successfully. Secret is no longer accessible."""


@dataclass
@PayloadRegistry.register
class DeleteSecretValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Secret value deletion failed. Common causes: key not found, access denied, deletion not allowed."""
