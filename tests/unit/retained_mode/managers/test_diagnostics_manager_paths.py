"""Tests for the report's list of where the engine reads and writes.

Every path here is shown redacted, and the manifest beside them says how many values were
hidden. That number is what a user checks the redaction against before attaching a bundle to
a public issue, so a path counted twice makes the manifest wrong in the direction nobody can
verify: it claims more was hidden than there was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.common.diagnostics.redaction import RedactionReason, Redactor
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager

if TYPE_CHECKING:
    from pathlib import Path

_MODULE = "griptape_nodes.retained_mode.managers.diagnostics_manager"

# The paths a report lists that this fixture controls. `libraries_directory` and
# `static_files_directory` are left unset, so they resolve to None and are neither shown nor
# counted.
_CONTROLLED_PATH_COUNT = 6


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory nothing exists under, so every path a report lists is missing.

    Set before any `Redactor` is built: a redactor reads the home directory once, when it is
    constructed, to work out what to replace with `~`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _manager(home: Path) -> DiagnosticsManager:
    """A manager whose every configured path sits under `home` and does not exist."""
    engine = Mock()
    engine.config_manager.workspace_path = home / "workspace"
    engine.config_manager.log_directory = home / "logs"
    engine.secrets_manager.workspace_env_path = home / "workspace" / ".env"
    engine.config_manager.get_config_value.return_value = None
    return DiagnosticsManager(Mock(), engine=engine)


class TestPathsSection:
    def test_a_path_that_is_both_listed_and_missing_is_counted_once(self, home: Path) -> None:
        """It is redacted once and the result reused for both places it appears.

        A redactor counts every match it makes, so redacting a path a second time to put it
        in the missing list counted its home directory twice. Worst for the workspace
        directory, which is both always listed and, on a machine worth collecting a bundle
        from, often the missing one.
        """
        redactor = Redactor()
        manager = _manager(home)

        with (
            patch(f"{_MODULE}.USER_CONFIG_PATH", home / "config" / "griptape_nodes_config.json"),
            patch(f"{_MODULE}.ENV_VAR_PATH", home / "config" / ".env"),
        ):
            paths = manager._build_paths_section(redactor, [])

        assert len(paths.missing_paths) == _CONTROLLED_PATH_COUNT
        assert redactor.counts() == {str(RedactionReason.HOME_DIRECTORY): _CONTROLLED_PATH_COUNT}

    def test_a_missing_path_is_listed_the_way_it_is_shown(self, home: Path) -> None:
        """The two lists have to agree, or nothing joins one to the other.

        The missing list is names, not keys, so a reader matches its entries against the
        paths above by string. A raw path there next to a redacted one above reads as two
        different directories -- and puts the home directory in a report that says it took it
        out.
        """
        redactor = Redactor()
        manager = _manager(home)

        with (
            patch(f"{_MODULE}.USER_CONFIG_PATH", home / "config" / "griptape_nodes_config.json"),
            patch(f"{_MODULE}.ENV_VAR_PATH", home / "config" / ".env"),
        ):
            paths = manager._build_paths_section(redactor, [])

        assert paths.workspace_directory in paths.missing_paths
        assert str(home) not in " ".join(paths.missing_paths)

    def test_a_path_setting_that_is_not_configured_is_neither_shown_nor_counted(self, home: Path) -> None:
        """None is the answer for a setting nobody wrote, and is not a path to redact."""
        redactor = Redactor()
        manager = _manager(home)

        with (
            patch(f"{_MODULE}.USER_CONFIG_PATH", home / "config" / "griptape_nodes_config.json"),
            patch(f"{_MODULE}.ENV_VAR_PATH", home / "config" / ".env"),
        ):
            paths = manager._build_paths_section(redactor, [])

        assert paths.libraries_directory is None
        assert paths.static_files_directory is None
        assert redactor.total_redactions() == _CONTROLLED_PATH_COUNT
