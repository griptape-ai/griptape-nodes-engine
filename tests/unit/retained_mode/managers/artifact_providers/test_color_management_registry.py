"""Tests for ColorManagementRegistry's first-registered-wins provider selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.managers.artifact_providers.base_color_management_provider import (
    BaseColorManagementProvider,
)
from griptape_nodes.retained_mode.managers.artifact_providers.color_management_registry import (
    ColorManagementRegistry,
)

if TYPE_CHECKING:
    import numpy as np
    import pytest


class _StubTransformRequest(RequestPayload):
    """A minimal RequestPayload stand-in for build_colorspace_transform_request()'s return value."""


class _StubColorManagementProvider(BaseColorManagementProvider):
    @classmethod
    def get_friendly_name(cls) -> str:
        return "Stub"

    @classmethod
    def build_colorspace_transform_request(
        cls,
        _pixels: np.ndarray,
        _source_colorspace: str,
        *,
        _display: str = "",
        _view: str = "",
        _config_path: str | None = None,
    ) -> RequestPayload:
        return _StubTransformRequest()

    @classmethod
    def list_colorspaces(cls, _config_path: str | None) -> list[str]:
        return ["ACEScg"]

    @classmethod
    def list_displays(cls, _config_path: str | None) -> list[str]:
        return ["sRGB"]

    @classmethod
    def list_views(cls, _config_path: str | None, _display: str) -> list[str]:
        return ["Standard"]


class _AlternateColorManagementProvider(BaseColorManagementProvider):
    @classmethod
    def get_friendly_name(cls) -> str:
        return "Alternate"

    @classmethod
    def build_colorspace_transform_request(
        cls,
        _pixels: np.ndarray,
        _source_colorspace: str,
        *,
        _display: str = "",
        _view: str = "",
        _config_path: str | None = None,
    ) -> RequestPayload:
        return _StubTransformRequest()

    @classmethod
    def list_colorspaces(cls, _config_path: str | None) -> list[str]:
        return []

    @classmethod
    def list_displays(cls, _config_path: str | None) -> list[str]:
        return []

    @classmethod
    def list_views(cls, _config_path: str | None, _display: str) -> list[str]:
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
