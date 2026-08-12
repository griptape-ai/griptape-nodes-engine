"""Resolve the credential used to authenticate against Griptape Cloud.

Griptape Cloud accepts two kinds of bearer credential: a Griptape Cloud API key
(``GT_CLOUD_API_KEY``, a ``gt-`` prefixed key) and a Griptape Nodes License
(``GRIPTAPE_NODES_LICENSE``, a JWT the desktop app writes into the engine's
global ``.env``). A license-only user has no API key at all, so any code path
that reads ``GT_CLOUD_API_KEY`` directly is unreachable for them.

Endpoints that accept a License, via the control plane's ``LicenseAuthMixin``:

- ``POST /api/v1/chat/completions`` (the sidebar agent's chat model)
- ``POST /api/images/generations`` (the agent's image-generation tool)
- ``/api/buckets*`` and ``/api/assets*`` (cloud storage and static files)
- ``GET /api/organizations``
- the model proxy (``/api/proxy/*``), which the standard library already handles
  in its own ``resolve_proxy_api_key``

Two endpoints must keep using the API key and must NOT be routed through here:

- ``GET /api/users`` has no ``LicenseAuthMixin``, so it answers a License with
  HTTP 401. Were one added, an unassigned license authenticates as the org's
  synthetic service principal, so "who is the current user" would answer with a
  service account rather than the human.
- ``wss://api.nodes.griptape.ai/ws/engines/events`` is served by a different
  service that accepts only ``gt-`` keys or Auth0-signed JWTs. A License is
  signed by the license key, so it fails signing-key lookup there.

Authenticating with a License is necessary but not always sufficient: the
control plane also evaluates an entitlement policy per request, so a License
that authenticates can still be refused with HTTP 403. See
:data:`POLICY_DENIED_HINT`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

LICENSE_SECRET_NAME = "GRIPTAPE_NODES_LICENSE"  # noqa: S105  # a secret's name, not a secret
"""Secret holding the Griptape Nodes License JWT. Written by the desktop app."""

API_KEY_SECRET_NAME = "GT_CLOUD_API_KEY"  # noqa: S105  # a secret's name, not a secret
"""Secret holding the Griptape Cloud API key."""

MISSING_CREDENTIAL_MESSAGE = (
    "no Griptape Cloud credential was found. Sign in with your Griptape license, "
    f"or set the {API_KEY_SECRET_NAME} secret in Settings."
)
"""User-facing reason to append to a "failed because" message.

Starts lowercase because every call site reads "Failed because ...".

Names both credentials, because telling a license-only user to set an API key
sends them after the wrong knob.
"""

_JWT_SEGMENT_COUNT = 3
"""A License is a JWT: header, payload, signature."""

POLICY_DENIED_HINT = (
    "your Griptape license or organization is not entitled to this action. "
    "Contact your Griptape administrator to request access."
)
"""User-facing reason for an HTTP 403 from Griptape Cloud."""


def resolve_cloud_credential(
    secrets_manager: SecretsManager | None = None,
    *,
    secret_name: str = API_KEY_SECRET_NAME,
) -> str | None:
    """Return the bearer credential for Griptape Cloud, or None if there is none.

    Resolution order, first non-empty wins:

    1. The Griptape Nodes License. Preferred over the API key so that a user who
       has both authenticates as their license, matching the standard library's
       ``resolve_proxy_api_key``.
    2. The Griptape Cloud API key (``secret_name``).

    Deliberately does NOT honor ``GT_CLOUD_PROXY_API_KEY``, even though the
    library's proxy resolver checks it first. That variable is scoped to the
    model proxy specifically ("without affecting other engine systems that use
    GT_CLOUD_API_KEY"), and the engine does not read its ``GT_CLOUD_PROXY_BASE_URL``
    companion. Honoring it here would send a proxy-only credential (e.g. the
    ``local`` value used against local proxy infra) to production Cloud for
    storage, sync, and bucket calls.

    Args:
        secrets_manager: Used to read secrets so that workspace and global
            ``.env`` files are searched, not just the environment. When omitted,
            only environment variables are consulted. Note that this is a
            boot-time snapshot: ``SecretsManager.__init__`` hydrates ``os.environ``
            from the ``.env`` files once, and the desktop app rewrites the license
            in that file without notifying the engine, so an env-only caller can
            hold a stale license until the process restarts.
        secret_name: Secret to fall back to for the API key.

    Returns:
        The credential, or None when neither a license nor an API key is set.
        Callers decide whether that is fatal; see
        :data:`MISSING_CREDENTIAL_MESSAGE` for the user-facing wording.
    """
    if secrets_manager is None:
        return os.getenv(LICENSE_SECRET_NAME) or os.getenv(secret_name)

    license_token = secrets_manager.get_secret(LICENSE_SECRET_NAME, should_error_on_not_found=False)
    if license_token:
        return license_token

    return secrets_manager.get_secret(secret_name, should_error_on_not_found=False)


def is_license_credential(credential: str | None) -> bool:
    """Return whether a credential is a License rather than a Griptape Cloud API key.

    Tests positively for a JWT's three dot-separated segments rather than merely
    "does not start with ``gt-``". Callers use this to attribute an HTTP 403 to a
    licensing decision, so a negative test would blame licensing for any
    unrecognized credential (a truncated paste, a future key format) and send the
    user to their administrator for an entitlement they already have.
    """
    if not credential:
        return False
    if credential.startswith("gt-"):
        return False
    return len(credential.split(".")) == _JWT_SEGMENT_COUNT
