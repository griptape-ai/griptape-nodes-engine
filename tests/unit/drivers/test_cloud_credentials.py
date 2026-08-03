"""Griptape Cloud credential resolution: license first, then the API key."""

from __future__ import annotations

import pytest

from griptape_nodes.drivers.cloud_credentials import (
    API_KEY_SECRET_NAME,
    LICENSE_SECRET_NAME,
    is_license_credential,
    resolve_cloud_credential,
)

_OTHER_SECRET_NAME = "OTHER_KEY"  # noqa: S105  # a secret's name, not a secret
_PROXY_API_KEY_ENV_VAR = "GT_CLOUD_PROXY_API_KEY"  # a secret's name, not a secret
_LICENSE = "eyJhbGciOiJFZERTQSJ9.eyJvcmdfaWQiOiJvIn0.sig"


class _FakeSecretsManager:
    """Stands in for SecretsManager, which reads .env files off disk."""

    def __init__(self, secrets: dict[str, str | None]) -> None:
        self._secrets = secrets

    def get_secret(self, secret_name: str, *, should_error_on_not_found: bool = True) -> str | None:  # noqa: ARG002
        return self._secrets.get(secret_name)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the developer's own Griptape Cloud credentials."""
    for var in (_PROXY_API_KEY_ENV_VAR, LICENSE_SECRET_NAME, API_KEY_SECRET_NAME):
        monkeypatch.delenv(var, raising=False)


class TestResolveCloudCredential:
    """Resolution order and the missing-credential case."""

    def test_resolves_license_when_api_key_absent(self) -> None:
        """The reported case: an Enterprise license, no GT_CLOUD_API_KEY."""
        secrets = _FakeSecretsManager({LICENSE_SECRET_NAME: _LICENSE})

        assert resolve_cloud_credential(secrets) == _LICENSE  # type: ignore[arg-type]

    def test_prefers_license_over_api_key(self) -> None:
        """With both configured the license wins, matching the standard library."""
        secrets = _FakeSecretsManager({LICENSE_SECRET_NAME: _LICENSE, API_KEY_SECRET_NAME: "gt-the-api-key"})

        assert resolve_cloud_credential(secrets) == _LICENSE  # type: ignore[arg-type]

    def test_falls_back_to_api_key(self) -> None:
        """Today's normal setup keeps working."""
        secrets = _FakeSecretsManager({API_KEY_SECRET_NAME: "gt-the-api-key"})

        assert resolve_cloud_credential(secrets) == "gt-the-api-key"  # type: ignore[arg-type]

    def test_ignores_proxy_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GT_CLOUD_PROXY_API_KEY is proxy-scoped and must not hijack engine calls.

        A proxy developer points that var at local infra (e.g. "local"). Honoring
        it here would send that credential to production Cloud for storage, sync,
        and bucket calls, which is a regression for an API-key-only user.
        """
        monkeypatch.setenv(_PROXY_API_KEY_ENV_VAR, "local")
        secrets = _FakeSecretsManager({API_KEY_SECRET_NAME: "gt-the-api-key"})

        assert resolve_cloud_credential(secrets) == "gt-the-api-key"  # type: ignore[arg-type]

    def test_blank_license_falls_through_to_api_key(self) -> None:
        """First non-empty wins: a hand-edited empty license must not shadow the key."""
        secrets = _FakeSecretsManager({LICENSE_SECRET_NAME: "", API_KEY_SECRET_NAME: "gt-the-api-key"})

        assert resolve_cloud_credential(secrets) == "gt-the-api-key"  # type: ignore[arg-type]

    def test_returns_none_when_nothing_configured(self) -> None:
        """Callers decide whether a missing credential is fatal."""
        assert resolve_cloud_credential(_FakeSecretsManager({})) is None  # type: ignore[arg-type]

    def test_honors_custom_secret_name(self) -> None:
        """A caller with its own API-key secret name still gets the license first."""
        secrets = _FakeSecretsManager({_OTHER_SECRET_NAME: "gt-other"})

        assert resolve_cloud_credential(secrets, secret_name=_OTHER_SECRET_NAME) == "gt-other"  # type: ignore[arg-type]

    def test_without_secrets_manager_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Drivers built straight from the environment are license-aware too."""
        monkeypatch.setenv(LICENSE_SECRET_NAME, _LICENSE)
        monkeypatch.setenv(API_KEY_SECRET_NAME, "gt-env-key")

        assert resolve_cloud_credential() == _LICENSE

    def test_without_secrets_manager_falls_back_to_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment-only resolution still honors the API key."""
        monkeypatch.setenv(API_KEY_SECRET_NAME, "gt-env-key")

        assert resolve_cloud_credential() == "gt-env-key"


class TestIsLicenseCredential:
    """Telling a license apart from anything else, to attribute an HTTP 403.

    Only a real JWT counts. A negative "not `gt-`" test would blame licensing for
    a malformed or future-format credential and send the user to their
    administrator for an entitlement they already have.
    """

    @pytest.mark.parametrize(
        ("credential", "expected"),
        [
            ("eyJhbGciOiJSUzI1NiJ9.payload.sig", True),
            ("gt-abc123", False),
            (None, False),
            ("", False),
            # Not a license: must not be reported as an entitlement denial.
            ("local", False),
            ("eyJhbGciOiJSUzI1NiJ9.truncated", False),
            ("has.four.dot.segments", False),
        ],
    )
    def test_classifies_credential(self, credential: str | None, expected: bool) -> None:  # noqa: FBT001
        """Only a three-segment JWT counts as a license."""
        assert is_license_credential(credential) is expected
