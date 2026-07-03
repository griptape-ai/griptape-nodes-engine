"""Tests for the PerPlatformProjectPath model and select_project_path helper."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from griptape_nodes.common.project_templates import (
    PerPlatformProjectPath,
    select_project_path,
)


class TestPerPlatformProjectPathModel:
    """Direct model behavior: validation, key requirements, extra-key rejection."""

    def test_at_least_one_key_required(self) -> None:
        with pytest.raises(ValidationError):
            PerPlatformProjectPath()

    def test_extra_keys_forbidden(self) -> None:
        # Common typo: `osx` instead of `darwin`. Schema must reject so the
        # admin sees a validation error rather than a silently-skipped path.
        with pytest.raises(ValidationError):
            PerPlatformProjectPath.model_validate({"osx": "/Volumes/p.yml", "windows": "Z:\\p.yml"})

    def test_accepts_single_key(self) -> None:
        model = PerPlatformProjectPath(darwin="/Volumes/p.yml")
        assert model.darwin == "/Volumes/p.yml"
        assert model.linux is None
        assert model.windows is None
        assert model.default is None

    def test_accepts_only_default(self) -> None:
        model = PerPlatformProjectPath(default="/srv/p.yml")
        assert model.default == "/srv/p.yml"


class TestPerPlatformProjectPathSelect:
    """`.select()` behavior: active-platform pick, default fallback, no-match."""

    def test_select_returns_active_platform_value_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        model = PerPlatformProjectPath(linux="/mnt/p.yml", darwin="/Volumes/p.yml", windows="Z:\\p.yml")
        assert model.select() == "/mnt/p.yml"

    def test_select_returns_active_platform_value_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        model = PerPlatformProjectPath(linux="/mnt/p.yml", darwin="/Volumes/p.yml", windows="Z:\\p.yml")
        assert model.select() == "/Volumes/p.yml"

    def test_select_returns_active_platform_value_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        model = PerPlatformProjectPath(linux="/mnt/p.yml", darwin="/Volumes/p.yml", windows="Z:\\p.yml")
        assert model.select() == "Z:\\p.yml"

    def test_select_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        model = PerPlatformProjectPath(darwin="/Volumes/p.yml", default="/srv/p.yml")
        assert model.select() == "/srv/p.yml"

    def test_select_returns_none_when_no_match_and_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        model = PerPlatformProjectPath(darwin="/Volumes/p.yml", windows="Z:\\p.yml")
        assert model.select() is None


class TestSelectProjectPathHelper:
    """`select_project_path` reduces the union to a single string."""

    def test_none_returns_none(self) -> None:
        assert select_project_path(None) is None

    def test_string_passthrough(self) -> None:
        assert select_project_path("/some/path.yml") == "/some/path.yml"

    def test_per_platform_object_selects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        model = PerPlatformProjectPath(darwin="/Volumes/p.yml", linux="/mnt/p.yml")
        assert select_project_path(model) == "/Volumes/p.yml"

    def test_per_platform_object_returns_none_when_no_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        model = PerPlatformProjectPath(darwin="/Volumes/p.yml")
        assert select_project_path(model) is None
