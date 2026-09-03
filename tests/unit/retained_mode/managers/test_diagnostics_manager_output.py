"""Tests for where a diagnostics bundle goes and what the engine says it was called.

These are the parts a user checks the moment collection finishes: the path printed in the
terminal and the name in the success message. Both were wrong in ways that are invisible
until someone goes looking for the file — a relative `--output` tested against one
directory and written to another, and a name reported as requested when the static files
manager had saved it under a different one.

`_cloud_api_key` is here for the same reason: reading a secret resolves the workspace, and
a missing workspace is exactly the thing these checks are collected to report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

_BUNDLE_NAME = "griptape-nodes-diagnostics-0.1.0-20260101.zip"


@pytest.fixture
def manager() -> DiagnosticsManager:
    """A manager with a stand-in engine, since these paths never reach one."""
    return DiagnosticsManager(Mock(), engine=cast("Engine", Mock()))


class TestResolveBundleDestination:
    def test_a_directory_gets_the_generated_file_name(self, manager: DiagnosticsManager, tmp_path: Path) -> None:
        resolved = manager._resolve_bundle_destination(str(tmp_path), _BUNDLE_NAME)

        assert resolved == tmp_path / _BUNDLE_NAME

    def test_a_file_name_is_used_as_given(self, manager: DiagnosticsManager, tmp_path: Path) -> None:
        requested = tmp_path / "for-the-bug-report.zip"

        assert manager._resolve_bundle_destination(str(requested), _BUNDLE_NAME) == requested

    def test_a_relative_directory_is_anchored_to_the_working_directory(
        self, manager: DiagnosticsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented default is the current directory, and `-o .` has to mean that.

        Left relative, the directory test ran against the working directory while the write
        ran against the workspace, so the bundle landed somewhere the user was not told.
        """
        monkeypatch.chdir(tmp_path)

        resolved = manager._resolve_bundle_destination(".", _BUNDLE_NAME)

        assert resolved.is_absolute()
        assert resolved == tmp_path / _BUNDLE_NAME

    def test_a_relative_file_name_is_anchored_to_the_working_directory(
        self, manager: DiagnosticsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        resolved = manager._resolve_bundle_destination("bundle.zip", _BUNDLE_NAME)

        assert resolved == tmp_path / "bundle.zip"

    def test_a_tilde_is_expanded(self, manager: DiagnosticsManager) -> None:
        resolved = manager._resolve_bundle_destination("~/bundle.zip", _BUNDLE_NAME)

        assert "~" not in str(resolved)
        assert resolved.name == "bundle.zip"


class TestFileNameFromUrl:
    def test_reports_the_name_the_file_was_actually_saved_under(self, manager: DiagnosticsManager) -> None:
        """Bundles are saved with CREATE_NEW, so a second one becomes `..._1.zip`."""
        url = f"https://static.example.com/files/{_BUNDLE_NAME.removesuffix('.zip')}_1.zip"

        assert manager._file_name_from_url(url, fallback=_BUNDLE_NAME).endswith("_1.zip")

    def test_ignores_the_query_string_a_signed_url_carries(self, manager: DiagnosticsManager) -> None:
        url = f"https://static.example.com/files/{_BUNDLE_NAME}?X-Amz-Signature=deadbeef&expires=99"

        assert manager._file_name_from_url(url, fallback="wrong.zip") == _BUNDLE_NAME

    def test_decodes_a_percent_encoded_name(self, manager: DiagnosticsManager) -> None:
        url = "https://static.example.com/files/my%20bundle.zip"

        assert manager._file_name_from_url(url, fallback="wrong.zip") == "my bundle.zip"

    def test_falls_back_when_the_url_points_at_no_file(self, manager: DiagnosticsManager) -> None:
        """A URL shape nobody expected must not make the success message empty."""
        assert manager._file_name_from_url("https://static.example.com/", fallback=_BUNDLE_NAME) == _BUNDLE_NAME


class TestCloudApiKey:
    def test_returns_the_key_the_connection_check_needs(self) -> None:
        engine = Mock()
        engine.secrets_manager.get_secret.return_value = "a-key"
        manager = DiagnosticsManager(Mock(), engine=cast("Engine", engine))

        assert manager._cloud_api_key() == "a-key"

    def test_a_workspace_that_has_gone_missing_costs_one_check_not_the_whole_run(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reading a secret resolves the workspace, which is what these checks report on."""
        engine = Mock()
        engine.secrets_manager.get_secret.side_effect = OSError("the workspace directory is gone")
        manager = DiagnosticsManager(Mock(), engine=cast("Engine", engine))

        with caplog.at_level("WARNING", logger="griptape_nodes"):
            key = manager._cloud_api_key()

        assert key is None
        assert "Griptape Cloud API key" in caplog.text
