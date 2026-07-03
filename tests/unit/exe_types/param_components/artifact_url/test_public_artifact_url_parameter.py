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
