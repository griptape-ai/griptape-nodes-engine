"""Tests for PublicArtifactUrlParameter.get_public_url_for_parameter input handling.

These cover the artifact shapes that reach the component in practice -- serialized
artifact dicts (orchestrator <-> worker JSON boundary) and ErrorArtifact propagated
from an upstream failure -- in addition to the original UrlArtifact / bare-string
paths. See griptape-ai/griptape-nodes-engine#4688.
"""

from typing import Any, NamedTuple
from unittest.mock import Mock

import pytest
from griptape.artifacts import ErrorArtifact
from griptape.artifacts.image_url_artifact import ImageUrlArtifact

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.param_components.artifact_url.public_artifact_url_parameter import (
    PublicArtifactUrlParameter,
)
from tests.unit.exe_types.mocks import MockNode

PUBLIC_URL = "https://cloud.example/public/artifact.png"


class ComponentFixture(NamedTuple):
    component: PublicArtifactUrlParameter
    driver: Mock


def _make_component(value: Any, *, param_type: str = "ImageUrlArtifact") -> ComponentFixture:
    """Build a component without running __init__ (which performs network calls)."""
    node = MockNode()
    parameter = Parameter(name="image", type=param_type, tooltip="t")
    node.add_parameter(parameter)
    node.parameter_values["image"] = value

    driver = Mock()
    driver.upload_file.return_value = PUBLIC_URL

    component = PublicArtifactUrlParameter.__new__(PublicArtifactUrlParameter)
    component._node = node
    component._parameter = parameter
    component._storage_driver = driver
    component.gtc_file_path = None
    return ComponentFixture(component=component, driver=driver)


class TestGetPublicUrlForParameter:
    def test_already_public_string_passes_through(self) -> None:
        component, driver = _make_component("https://example.com/img.png")

        assert component.get_public_url_for_parameter() == "https://example.com/img.png"
        driver.upload_file.assert_not_called()

    def test_url_artifact_with_public_url_passes_through(self) -> None:
        component, driver = _make_component(ImageUrlArtifact(value="https://example.com/img.png"))

        assert component.get_public_url_for_parameter() == "https://example.com/img.png"
        driver.upload_file.assert_not_called()

    def test_serialized_url_artifact_dict_is_hydrated(self) -> None:
        # A value that crossed a JSON boundary arrives as a dict, not an artifact.
        component, driver = _make_component(ImageUrlArtifact(value="https://example.com/img.png").to_dict())

        assert component.get_public_url_for_parameter() == "https://example.com/img.png"
        driver.upload_file.assert_not_called()

    def test_error_artifact_raises_with_upstream_message(self) -> None:
        component, driver = _make_component(ErrorArtifact(value="upstream blew up"))

        with pytest.raises(RuntimeError, match="upstream blew up") as excinfo:
            component.get_public_url_for_parameter()

        # The error should name the parameter so the editor points at the real cause.
        assert "image" in str(excinfo.value)
        driver.upload_file.assert_not_called()


class TestGetBucketId:
    """Covers GT_CLOUD_BUCKET_ID resolution -- see griptape-ai/griptape-nodes-engine#5074.

    A blank or invalid bucket secret used to be returned verbatim and only failed later
    as an opaque 404 from a `/api/buckets//assets/...` URL. These tests pin the fail-fast
    behavior: a valid ID passes through, a blank one falls back to auto-select, and an
    invalid ID raises a clear, actionable error.

    A configured ID is validated with a direct `bucket_exists` GET rather than scanning
    the paginated `list_buckets` result, so a valid bucket beyond the first page is not
    mistaken for a missing one. When the secret is unset/blank, the fallback is the
    organization's default bucket -- guaranteed to exist and undeletable -- not the first
    entry of a paginated bucket list.
    """

    MODULE = "griptape_nodes.exe_types.param_components.artifact_url.public_artifact_url_parameter"

    def _patch(
        self,
        mocker: Any,
        *,
        bucket_id_value: str | None,
        bucket_exists: bool = True,
        default_bucket_id: str | None = None,
    ) -> tuple[Mock, Mock]:
        mocker.patch.object(PublicArtifactUrlParameter, "_get_secret_value", return_value=bucket_id_value)
        exists_mock = mocker.patch(
            f"{self.MODULE}.GriptapeCloudStorageDriver.bucket_exists",
            return_value=bucket_exists,
        )
        default_mock = mocker.patch(
            f"{self.MODULE}.GriptapeCloudStorageDriver.get_default_bucket_id",
            return_value=default_bucket_id,
        )
        return exists_mock, default_mock

    def test_valid_bucket_id_passes_through(self, mocker: Any) -> None:
        exists_mock, default_mock = self._patch(mocker, bucket_id_value="bucket-123", bucket_exists=True)

        assert PublicArtifactUrlParameter._get_bucket_id("https://base", "key") == "bucket-123"
        # A configured ID is validated directly; the org default is never consulted.
        exists_mock.assert_called_once()
        default_mock.assert_not_called()

    def test_valid_bucket_id_beyond_first_page_still_validates(self, mocker: Any) -> None:
        # Regression: the bucket exists but is not on the default `list_buckets` page.
        # `bucket_exists` (a direct GET) must be the source of truth, not the list.
        self._patch(mocker, bucket_id_value="page-2-bucket", bucket_exists=True)

        assert PublicArtifactUrlParameter._get_bucket_id("https://base", "key") == "page-2-bucket"

    def test_unset_secret_falls_back_to_org_default_bucket(self, mocker: Any) -> None:
        self._patch(mocker, bucket_id_value=None, default_bucket_id="org-default")

        assert PublicArtifactUrlParameter._get_bucket_id("https://base", "key") == "org-default"

    def test_blank_secret_falls_back_to_org_default_bucket(self, mocker: Any) -> None:
        self._patch(mocker, bucket_id_value="   ", default_bucket_id="org-default")

        assert PublicArtifactUrlParameter._get_bucket_id("https://base", "key") == "org-default"

    def test_invalid_bucket_id_raises_clear_error(self, mocker: Any) -> None:
        self._patch(mocker, bucket_id_value="does-not-exist", bucket_exists=False)

        with pytest.raises(RuntimeError, match="invalid bucket ID") as excinfo:
            PublicArtifactUrlParameter._get_bucket_id("https://base", "key")

        message = str(excinfo.value)
        assert PublicArtifactUrlParameter.BUCKET_ID_NAME in message
        assert "does-not-exist" in message

    def test_blank_secret_with_no_default_bucket_names_the_secret(self, mocker: Any) -> None:
        self._patch(mocker, bucket_id_value="", default_bucket_id=None)

        with pytest.raises(RuntimeError, match=PublicArtifactUrlParameter.BUCKET_ID_NAME):
            PublicArtifactUrlParameter._get_bucket_id("https://base", "key")

    def test_unset_secret_with_no_default_bucket_raises_original_message(self, mocker: Any) -> None:
        self._patch(mocker, bucket_id_value=None, default_bucket_id=None)

        with pytest.raises(RuntimeError, match="No Griptape Cloud storage buckets found"):
            PublicArtifactUrlParameter._get_bucket_id("https://base", "key")
