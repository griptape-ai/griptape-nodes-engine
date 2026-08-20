"""Unit tests for the os_utils platform-detection predicates."""

import pytest

from griptape_nodes.files import os_utils


class TestPlatformPredicates:
    """Tests for is_windows / is_mac / is_linux.

    Each predicate is exercised for both a matching and a non-matching
    ``sys.platform`` value so the tests run identically on any host OS.
    """

    def test_is_windows_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "win32")
        assert os_utils.is_windows() is True
        assert os_utils.is_mac() is False
        assert os_utils.is_linux() is False

    def test_is_mac_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "darwin")
        assert os_utils.is_mac() is True
        assert os_utils.is_windows() is False
        assert os_utils.is_linux() is False

    def test_is_linux_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "linux")
        assert os_utils.is_linux() is True
        assert os_utils.is_windows() is False
        assert os_utils.is_mac() is False


class TestOSDisplayName:
    """Tests for os_display_name, which names the OS the way a user would.

    Values are asserted against a patched ``sys.platform`` so the tests run identically on
    any host OS.
    """

    def test_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "win32")
        assert os_utils.os_display_name() == "Windows"

    def test_mac(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "darwin" is the one a user would never recognize, so it is the point of the mapping.
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "darwin")
        assert os_utils.os_display_name() == "macOS"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "linux")
        assert os_utils.os_display_name() == "Linux"

    def test_unrecognized_platform_falls_back_to_sys_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Something to show beats showing nothing, and it is never empty.
        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "freebsd14")
        assert os_utils.os_display_name() == "freebsd14"


class TestOSManagerDelegates:
    """OSManager's static predicates delegate to os_utils (single source of truth)."""

    def test_os_manager_delegates_to_os_utils(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from griptape_nodes.retained_mode.managers.os_manager import OSManager

        monkeypatch.setattr("griptape_nodes.files.os_utils.sys.platform", "win32")
        assert OSManager.is_windows() is True
        assert OSManager.is_mac() is False
        assert OSManager.is_linux() is False
