"""Tests for ColorManagementRegistry's first-registered-wins provider selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.managers.artifact_providers.base_color_management_provider import (
    BaseColorManagementProvider,
    ColorTransformTarget,
)
from griptape_nodes.retained_mode.managers.artifact_providers.color_management_registry import (
    ColorManagementRegistry,
)

if TYPE_CHECKING:
    from typing import Any

    import numpy as np
    import pytest

    from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import ImageArtifactSituation


class _StubTransformRequest(RequestPayload):
    """A minimal RequestPayload stand-in for build_transform_request()'s return value."""


class _StubColorManagementProvider(BaseColorManagementProvider):
    @classmethod
    def get_friendly_name(cls) -> str:
        return "Stub"

    @classmethod
    def build_transform_request(
        cls,
        _pixels: np.ndarray,
        _source_colorspace: str,
        _situation: ImageArtifactSituation,
        *,
        _provider_data: dict[str, Any] | None = None,
    ) -> RequestPayload:
        return _StubTransformRequest()

    @classmethod
    def list_colorspaces(cls, _provider_data: dict[str, Any] | None = None) -> list[str]:
        return ["ACEScg"]

    @classmethod
    def list_transform_targets(cls, _provider_data: dict[str, Any] | None = None) -> list[ColorTransformTarget]:
        return [ColorTransformTarget("srgb/standard", "sRGB")]


class _AlternateColorManagementProvider(BaseColorManagementProvider):
    @classmethod
    def get_friendly_name(cls) -> str:
        return "Alternate"

    @classmethod
    def build_transform_request(
        cls,
        _pixels: np.ndarray,
        _source_colorspace: str,
        _situation: ImageArtifactSituation,
        *,
        _provider_data: dict[str, Any] | None = None,
    ) -> RequestPayload:
        return _StubTransformRequest()

    @classmethod
    def list_colorspaces(cls, _provider_data: dict[str, Any] | None = None) -> list[str]:
        return []

    @classmethod
    def list_transform_targets(cls, _provider_data: dict[str, Any] | None = None) -> list[ColorTransformTarget]:
        return []


class TestColorManagementRegistry:
    def test_get_registered_provider_returns_none_when_nothing_registered(self) -> None:
        registry = ColorManagementRegistry()

        assert registry.get_registered_provider() is None

    def test_register_then_get_returns_the_provider(self) -> None:
        registry = ColorManagementRegistry()

        registry.register_provider(_StubColorManagementProvider)

        assert registry.get_registered_provider() is _StubColorManagementProvider

    def test_second_registration_logs_warning_and_keeps_first(self, caplog: pytest.LogCaptureFixture) -> None:
        registry = ColorManagementRegistry()
        registry.register_provider(_StubColorManagementProvider)

        with caplog.at_level("WARNING"):
            registry.register_provider(_AlternateColorManagementProvider)

        assert registry.get_registered_provider() is _StubColorManagementProvider
        assert any("already registered" in record.message for record in caplog.records)
